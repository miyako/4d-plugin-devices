import contextlib
import importlib.util
import io
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lifecycle", ROOT / "scripts/lifecycle.py")
lc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lc)
REPOSITORY = "miyako/4d-plugin-devices"
FIXTURE_VERSION = "2.7.9"


def fixture():
    lc.PROJECT.parent.mkdir(parents=True, exist_ok=True)
    lc.PROJECT.write_text(
        "\n".join(
            f"{key} = {FIXTURE_VERSION};"
            for _ in range(3) for key in lc.KEYS
        ) + "\n"
    )
    lc.PLIST.write_bytes(plistlib.dumps({
        "CFBundleShortVersionString": FIXTURE_VERSION,
        "CFBundleVersion": FIXTURE_VERSION,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleExecutable": "$(PRODUCT_NAME)",
    }))


def release(number, tag=None, draft=False):
    return {
        "id": number, "tag_name": tag or f"v0.0.{number}",
        "draft": draft, "prerelease": False,
    }


class DirectoryTest(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.directory = tempfile.TemporaryDirectory()
        os.chdir(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(os.chdir, self.previous)
        fixture()


class SourceTests(DirectoryTest):
    def test_patch_minor_major_stable_fixtures(self):
        for mode, expected in (
            ("patch", "2.7.10"), ("minor", "2.8.0"), ("major", "3.0.0")
        ):
            with self.subTest(mode=mode):
                fixture()
                self.assertEqual(lc.bump(mode), expected)
                self.assertEqual(lc.source_version(), expected)
                data = plistlib.loads(lc.PLIST.read_bytes())
                self.assertEqual(data["CFBundleVersion"], expected)
                self.assertEqual(data["CFBundleInfoDictionaryVersion"], "6.0")
                self.assertEqual(data["CFBundleExecutable"], "$(PRODUCT_NAME)")

    def test_invalid_or_drifted_project_rejected_without_writes(self):
        for bad in ("2.7.8", "02.7.9", "2.7", "2.7.9-beta", "abc", "",
                    "$(UNKNOWN)", '""2.7.9"', "2.7.9\n"):
            for key in lc.KEYS:
                with self.subTest(bad=bad, key=key):
                    fixture()
                    lc.PROJECT.write_text(lc.PROJECT.read_text().replace(
                        f"{key} = {FIXTURE_VERSION};", f"{key} = {bad};", 1
                    ))
                    before = [path.read_bytes() for path in lc.SOURCES]
                    with self.assertRaises(ValueError):
                        lc.bump("patch")
                    self.assertEqual(before, [path.read_bytes() for path in lc.SOURCES])

    def test_missing_project_key(self):
        lc.PROJECT.write_text("MARKETING_VERSION = 2.7.9;\n")
        with self.assertRaises(ValueError):
            lc.source_version()

    def test_plist_drift_invalid_and_unknown_reference(self):
        for key in ("CFBundleShortVersionString", "CFBundleVersion"):
            for value in ("2.7.8", "2.7", "02.7.9", "$(UNKNOWN)", 123, ""):
                with self.subTest(key=key, value=value):
                    fixture()
                    data = plistlib.loads(lc.PLIST.read_bytes())
                    data[key] = value
                    lc.PLIST.write_bytes(plistlib.dumps(data))
                    with self.assertRaises(ValueError):
                        lc.source_version()

    def test_missing_short_and_duplicate_key(self):
        lc.PLIST.write_bytes(plistlib.dumps({"CFBundleVersion": FIXTURE_VERSION}))
        with self.assertRaises(ValueError):
            lc.source_version()
        fixture()
        lc.PLIST.write_text(lc.PLIST.read_text().replace(
            "</dict>", "<key>CFBundleVersion</key><string>2.7.9</string></dict>"
        ))
        with self.assertRaises(ValueError):
            lc.source_version()

    def test_preserve_recognized_references(self):
        for reference in lc.REFERENCES:
            with self.subTest(reference=reference):
                fixture()
                data = plistlib.loads(lc.PLIST.read_bytes())
                data.update(CFBundleShortVersionString=reference, CFBundleVersion=reference)
                lc.PLIST.write_bytes(plistlib.dumps(data))
                lc.PROJECT.write_text(lc.PROJECT.read_text().replace(
                    "CURRENT_PROJECT_VERSION = 2.7.9;",
                    'CURRENT_PROJECT_VERSION = "$(MARKETING_VERSION)";'
                ))
                lc.bump("patch")
                self.assertEqual(lc.source_version(), "2.7.10")
                self.assertEqual(plistlib.loads(lc.PLIST.read_bytes()), data)
                self.assertIn('"$(MARKETING_VERSION)"', lc.PROJECT.read_text())

    def test_absent_bundle_version_stays_absent(self):
        data = plistlib.loads(lc.PLIST.read_bytes())
        del data["CFBundleVersion"]
        lc.PLIST.write_bytes(plistlib.dumps(data))
        lc.bump("major")
        self.assertNotIn("CFBundleVersion", plistlib.loads(lc.PLIST.read_bytes()))

    def test_live_source_uses_derived_expectation_and_no_prebuilt_changes(self):
        repository = ROOT.parent
        for path in lc.SOURCES:
            shutil.copyfile(repository / path, path)
        expected = lc.source_version()
        self.assertRegex(expected, "^" + lc.VERSION + "$")
        before = plistlib.loads(lc.PLIST.read_bytes())
        parts = list(map(int, expected.split(".")))
        parts[-1] += 1
        bumped = ".".join(map(str, parts))
        self.assertEqual(lc.bump("patch"), bumped)
        after = plistlib.loads(lc.PLIST.read_bytes())
        for key in before.keys() - {"CFBundleShortVersionString", "CFBundleVersion"}:
            self.assertEqual(before[key], after[key])
        self.assertEqual(lc.source_version(), bumped)


class AbsenceTests(unittest.TestCase):
    def test_paginated_draft_collision(self):
        pages = [[release(i) for i in range(1, 101)],
                 [release(101, "v2.7.9", draft=True)]]
        with patch.object(lc, "remote_ref", return_value=None), patch.object(
            lc, "api", side_effect=pages
        ) as api:
            with self.assertRaisesRegex(ValueError, "including drafts"):
                lc.assert_absent(REPOSITORY, "v2.7.9")
            self.assertIn("page=2", api.call_args.args[1])

    def test_only_complete_successful_listing_is_absent(self):
        with patch.object(lc, "remote_ref", return_value=None), patch.object(
            lc, "api", side_effect=[[release(i) for i in range(1, 101)], []]
        ) as api:
            lc.assert_absent(REPOSITORY, "v2.7.9")
            self.assertEqual(api.call_count, 2)

    def test_api_404_auth_network_and_later_page_errors_fail_closed(self):
        for message in ("HTTP 404", "HTTP 401", "HTTP 403", "network failure"):
            for later in (False, True):
                effects = ([release(i) for i in range(1, 101)],) if later else ()
                with self.subTest(message=message, later=later), patch.object(
                    lc, "remote_ref", return_value=None
                ), patch.object(lc, "api", side_effect=[
                    *effects, subprocess.CalledProcessError(1, ["gh"], stderr=message)
                ]):
                    with self.assertRaises(subprocess.CalledProcessError):
                        lc.assert_absent(REPOSITORY, "v2.7.9")

    def test_malformed_pages_and_records_fail_closed(self):
        for malformed in (
            {}, None, "[]", [None], [{}], [{"tag_name": "v1.0.0"}],
            [{**release(1), "draft": "false"}],
            [{**release(1), "id": True}],
            [{**release(1), "tag_name": None}],
            [release(1), release(1)], [release(i) for i in range(1, 102)]
        ):
            for later in (False, True):
                with self.subTest(page=malformed, later=later), patch.object(
                    lc, "remote_ref", return_value=None
                ), patch.object(lc, "api", side_effect=[
                    *([[release(i) for i in range(1000, 1100)]] if later else []),
                    malformed
                ]):
                    with self.assertRaises(ValueError):
                        lc.assert_absent(REPOSITORY, "v2.7.9")

    def test_existing_remote_tag_rejected_without_api(self):
        with patch.object(lc, "remote_ref", return_value="a" * 40), patch.object(
            lc, "api"
        ) as api:
            with self.assertRaisesRegex(ValueError, "already exists"):
                lc.assert_absent(REPOSITORY, "v2.7.9")
            api.assert_not_called()

    def test_remote_errors_not_absence(self):
        for code, output in ((1, ""), (128, ""), (2, "unexpected"), (0, "bad")):
            with self.subTest(code=code, output=output), patch.object(
                lc, "run", return_value=subprocess.CompletedProcess(
                    [], code, output, "failure"
                )
            ):
                with self.assertRaises(ValueError):
                    lc.remote_ref("refs/tags/v2.7.9")

    def test_api_json_and_authenticated_cli_contract(self):
        for output in ("not json", ""):
            with patch.object(lc, "run", return_value=subprocess.CompletedProcess(
                [], 0, output, ""
            )):
                with self.assertRaises(ValueError):
                    lc.api("GET", "repos/example/releases")
        with patch.object(lc, "run", return_value=subprocess.CompletedProcess(
            [], 0, "[]", ""
        )) as run:
            lc.api("GET", "repos/example/releases?per_page=100&page=2")
            self.assertEqual(run.call_args.args[:4], ("gh", "api", "--method", "GET"))


class GitTests(DirectoryTest):
    def git(self, *args):
        return lc.run("git", *args).stdout.strip()

    def setUp(self):
        super().setUp()
        remote_directory = tempfile.TemporaryDirectory()
        self.addCleanup(remote_directory.cleanup)
        self.remote = Path(remote_directory.name) / "remote.git"
        self.git("init", "--bare", str(self.remote))
        self.git("init", "-b", "selected")
        self.git("config", "user.name", "Lifecycle tests")
        self.git("config", "user.email", "tests@example.invalid")
        self.git("config", "push.followTags", "true")
        self.git("add", *(str(path) for path in lc.SOURCES))
        self.git("commit", "-m", "fixture")
        self.sha = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "--no-follow-tags", "origin", "HEAD:refs/heads/selected")
        self.git("tag", "-a", "unrelated-annotated", "-m", "must not follow")
        self.assets = ("Devices-2.7.9.zip", "Devices-2.7.9.dmg")
        for asset in self.assets:
            Path(asset).write_bytes(b"fixture asset")
        # Assets are untracked only for publication; bumps require a clean tree.

    def clean_assets(self):
        for asset in self.assets:
            Path(asset).unlink()

    def api_success(self, method, endpoint, payload=None):
        if method == "GET":
            return []
        self.assertEqual(method, "POST")
        self.assertEqual(endpoint, f"repos/{REPOSITORY}/releases")
        self.assertEqual(payload, {
            "tag_name": "v2.7.9", "target_commitish": self.sha, "name": "v2.7.9",
            "draft": False, "prerelease": False, "body": "",
        })
        return release(77, "v2.7.9")

    def test_bump_source_only_no_follow_tags(self):
        self.clean_assets()
        lc.bump_and_push("patch", "selected", self.sha)
        self.assertNotEqual(lc.remote_ref("refs/heads/selected"), self.sha)
        self.assertEqual(self.git("ls-remote", "--tags", "origin"), "")
        self.assertEqual(
            set(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()),
            {str(path) for path in lc.SOURCES}
        )
        self.assertEqual(lc.source_version(), "2.7.10")

    def test_bump_rejects_moved_branch_before_changes(self):
        self.clean_assets()
        self.git("commit", "--allow-empty", "-m", "other change")
        newer = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "moved"):
            lc.bump_and_push("patch", "selected", newer)
        self.assertEqual(lc.source_version(), FIXTURE_VERSION)

    def test_bump_rejects_receive_pack_branch_races(self):
        self.clean_assets()
        # Make an ancestor available to simulate even a backwards-moving branch.
        self.git("commit", "--allow-empty", "-m", "selected snapshot")
        selected = self.git("rev-parse", "HEAD")
        self.git("push", "--no-follow-tags", "origin", "HEAD:refs/heads/selected")
        original_run = lc.run
        for direction in ("backwards", "forwards"):
            with self.subTest(direction=direction):
                def race(*args, **kwargs):
                    if "push" in args:
                        target = self.sha if direction == "backwards" else self.git(
                            "rev-parse", "HEAD"
                        )
                        # Same-SHA forward race must also be rejected.
                        original_run("git", "push", "--no-follow-tags", "origin",
                                     f"{target}:refs/heads/race-object")
                        original_run("git", "--git-dir", str(self.remote), "update-ref",
                                     "refs/heads/selected", target)
                    return original_run(*args, **kwargs)
                with patch.object(lc, "run", side_effect=race):
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        lc.bump_and_push("patch", "selected", selected)
                self.assertEqual(self.git("ls-remote", "--tags", "origin"), "")
                self.assertEqual(lc.remote_ref("refs/heads/selected"),
                                 self.sha if direction == "backwards"
                                 else self.git("rev-parse", "HEAD"))
                # Restore only this temporary fixture, not the workspace.
                self.git("reset", "--soft", selected)
                fixture()
                self.git("add", *(str(path) for path in lc.SOURCES))
                self.git("--git-dir", str(self.remote), "update-ref",
                         "refs/heads/selected", selected)

    def publish_with_mock_gh(self, api=None, upload_failure=False, race=None):
        original = lc.run
        calls = []

        def commands(*args, **kwargs):
            calls.append(args)
            if args[:3] == ("gh", "release", "upload"):
                self.assertNotIn("--clobber", args)
                self.assertEqual(args[3:], ("v2.7.9", *self.assets, "--repo", REPOSITORY))
                if upload_failure:
                    raise subprocess.CalledProcessError(1, args, stderr="upload failed")
                return subprocess.CompletedProcess(args, 0, "", "")
            if "push" in args and race:
                original("git", "push", "--no-follow-tags", "origin",
                         f"{race}:refs/tags/v2.7.9")
            return original(*args, **kwargs)

        with patch.object(lc, "run", side_effect=commands), patch.object(
            lc, "api", side_effect=api or self.api_success
        ), contextlib.redirect_stdout(io.StringIO()):
            result = lc.publish(REPOSITORY, self.sha, self.assets)
        return result, calls

    def test_publication_creates_exact_tag_then_post_then_upload(self):
        tag, calls = self.publish_with_mock_gh()
        self.assertEqual(tag, "v2.7.9")
        self.assertEqual(lc.remote_ref("refs/tags/v2.7.9"), self.sha)
        self.assertIsNone(lc.remote_ref("refs/tags/unrelated-annotated"))
        push_index = next(i for i, args in enumerate(calls) if "push" in args)
        upload_index = next(i for i, args in enumerate(calls) if args[0] == "gh")
        self.assertLess(push_index, upload_index)
        self.assertIn("--no-follow-tags", calls[push_index])

    def test_same_sha_tag_creation_race_rejected(self):
        with self.assertRaisesRegex(ValueError, "not newly updated"):
            self.publish_with_mock_gh(race=self.sha)
        self.assertEqual(lc.remote_ref("refs/tags/v2.7.9"), self.sha)

    def test_different_sha_tag_creation_race_rejected(self):
        self.git("commit", "--allow-empty", "-m", "different")
        different = self.git("rev-parse", "HEAD")
        self.sha = different
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish_with_mock_gh(race=self.git("rev-parse", "HEAD^"))
        self.assertNotEqual(lc.remote_ref("refs/tags/v2.7.9"), different)

    def test_post_failure_retains_tag_and_never_uploads(self):
        methods = []

        def api(method, endpoint, payload=None):
            methods.append(method)
            if method == "GET":
                return []
            raise subprocess.CalledProcessError(1, ["gh"], stderr="POST failed")

        with self.assertRaises(subprocess.CalledProcessError):
            self.publish_with_mock_gh(api=api)
        self.assertEqual(methods, ["GET", "POST"])
        self.assertEqual(lc.remote_ref("refs/tags/v2.7.9"), self.sha)

    def test_malformed_post_response_retains_tag_without_upload(self):
        for malformed in (None, {}, {"id": 77}, release(77, "v9.9.9"),
                          {**release(77, "v2.7.9"), "draft": True}):
            with self.subTest(response=malformed):
                def api(method, endpoint, payload=None):
                    return [] if method == "GET" else malformed
                with self.assertRaisesRegex(ValueError, "Malformed newly-created"):
                    self.publish_with_mock_gh(api=api)
                self.assertEqual(lc.remote_ref("refs/tags/v2.7.9"), self.sha)
                # Removing only a local test fixture ref, never a remote API operation.
                self.git("--git-dir", str(self.remote), "update-ref", "-d", "refs/tags/v2.7.9")

    def test_final_absence_failure_blocks_tag_and_post(self):
        methods = []

        def api(method, endpoint, payload=None):
            methods.append(method)
            return [release(1, "v2.7.9", draft=True)]

        with self.assertRaisesRegex(ValueError, "already exists"):
            self.publish_with_mock_gh(api=api)
        self.assertEqual(methods, ["GET"])
        self.assertIsNone(lc.remote_ref("refs/tags/v2.7.9"))

    def test_tag_verification_failure_blocks_post_and_retains_tag(self):
        original = lc.remote_ref
        lookups = []

        def remote(ref):
            lookups.append(ref)
            return None if len(lookups) == 1 else "a" * 40

        with patch.object(lc, "remote_ref", side_effect=remote):
            with self.assertRaisesRegex(ValueError, "does not point"):
                self.publish_with_mock_gh()
        self.assertEqual(original("refs/tags/v2.7.9"), self.sha)

    def test_upload_failure_retains_public_release_and_tag(self):
        created = []

        def api(method, endpoint, payload=None):
            result = self.api_success(method, endpoint, payload)
            if method == "POST":
                created.append(result)
            return result

        with self.assertRaises(subprocess.CalledProcessError):
            self.publish_with_mock_gh(api=api, upload_failure=True)
        self.assertEqual(created, [release(77, "v2.7.9")])
        self.assertEqual(lc.remote_ref("refs/tags/v2.7.9"), self.sha)

    def test_wrong_sha_or_missing_asset_blocks_mutation(self):
        with patch.object(lc, "api") as api:
            with self.assertRaises(ValueError):
                lc.publish(REPOSITORY, "a" * 40, self.assets)
            Path(self.assets[0]).unlink()
            with self.assertRaises(ValueError):
                lc.publish(REPOSITORY, self.sha, self.assets)
            api.assert_not_called()
        self.assertIsNone(lc.remote_ref("refs/tags/v2.7.9"))

    def test_changed_source_blocks_publication(self):
        lc.bump("patch")
        with patch.object(lc, "api") as api:
            with self.assertRaises(subprocess.CalledProcessError):
                lc.publish(REPOSITORY, self.sha, self.assets)
            api.assert_not_called()
        self.assertIsNone(lc.remote_ref("refs/tags/v2.7.9"))

    def test_cli_retention_diagnostics(self):
        with patch.object(lc, "publish", side_effect=subprocess.CalledProcessError(
            1, ["gh"], stderr="POST failed"
        )), patch.dict(os.environ, GITHUB_REPOSITORY=REPOSITORY, GITHUB_SHA=self.sha), patch(
            "sys.argv", ["lifecycle.py", "publish", *self.assets]
        ), contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as result:
                lc.main()
        self.assertEqual(result.exception.code, 1)
        self.assertIn("POST failed", output.getvalue())
        self.assertIn("retained", output.getvalue())
        self.assertIn("do not retry blindly", output.getvalue())


class WorkflowTests(unittest.TestCase):
    def test_manual_zero_input_immutable_serialized_workflow(self):
        text = (ROOT / "workflows/release.yml").read_text()
        self.assertIn("name: Build\n", text)
        trigger = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(trigger.strip(), "workflow_dispatch:")
        self.assertNotIn("inputs.", text)
        self.assertEqual(text.count("uses: actions/checkout@v4"), 3)
        self.assertEqual(text.count("ref: ${{ github.sha }}"), 3)
        self.assertIn("group: release-publication\n", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("needs: [version, build-windows]", text)
        gates = (
            "check prebuilt libraries", "build macOS plugin (universal)",
            "verify universal binary", "merge Windows binaries into the bundle",
            "- name: codesign", "package, notarize & staple",
            'notarize "$ZIP_PATH"', 'notarize "$DMG_PATH"',
            'xcrun stapler staple "$DMG_PATH"',
            "lifecycle.py publish",
        )
        offsets = [text.index(gate) for gate in gates]
        self.assertEqual(offsets, sorted(offsets))
        self.assertIn("lifecycle.py check", text)
        for secret in (
            "APPLE_DEVELOPER_ID_CERTIFICATE", "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
            "KEYCHAIN_PASSWORD", "NOTARYTOOL_APPLE_ID", "NOTARYTOOL_TEAM_ID",
            "NOTARYTOOL_PASSWORD",
        ):
            self.assertIn("secrets." + secret, text)
        self.assertIn('ZIP_PATH="${PRODUCT}-${VERSION}.zip"', text)
        self.assertIn('DMG_PATH="${PRODUCT}-${VERSION}.dmg"', text)

    def test_bump_workflow_contract(self):
        text = (ROOT / "workflows/bump-version.yml").read_text()
        for mode in ("patch", "minor", "major"):
            self.assertIn(f"- {mode}", text)
        self.assertIn("ref: ${{ github.sha }}", text)
        self.assertIn('bump-push "$MODE" "$SELECTED_BRANCH" "$GITHUB_SHA"', text)
        self.assertIn('"$GITHUB_REF_TYPE" != branch', text)
        self.assertNotIn("git tag", text)

    def test_no_destructive_release_operations(self):
        helper = (ROOT / "scripts/lifecycle.py").read_text()
        workflow = (ROOT / "workflows/release.yml").read_text()
        for forbidden in (
            '"DELETE"', '"PATCH"', "--clobber", '"release", "create"',
            "action-gh-release", "--generate-notes",
        ):
            self.assertNotIn(forbidden, helper + workflow)
        self.assertNotIn("--force", helper)
        self.assertIn('"POST", f"repos/{repository}/releases"', helper)
        self.assertIn('"gh", "release", "upload"', helper)
        self.assertIn("Any created tag, release or assets are retained", helper)


if __name__ == "__main__":
    unittest.main()
