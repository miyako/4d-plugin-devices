"""Source-only version bumps and fail-closed, create-only publication."""

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile


PROJECT = Path("Devices.xcodeproj/project.pbxproj")
PLIST = Path("Info.plist")
SOURCES = (PROJECT, PLIST)
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
KEYS = ("MARKETING_VERSION", "CURRENT_PROJECT_VERSION")
ASSIGNMENT = re.compile(r"\b(" + "|".join(KEYS) + r")\s*=\s*([^;\n]*);")
REFERENCES = {
    f"{opening}{key}{closing}": key
    for key in KEYS
    for opening, closing in (("$(", ")"), ("${", "}"))
}


def run(*args, check=True, **kwargs):
    return subprocess.run(
        args, check=check, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, **kwargs
    )


def setting_value(value):
    value = value.strip()
    if value.startswith('"'):
        if not re.fullmatch(r'"[^"]+"', value):
            raise ValueError(f"Malformed quoted setting: {value!r}")
        return value[1:-1]
    return value


def source_version():
    text = PROJECT.read_text()
    fields = {key: [] for key in KEYS}
    for match in ASSIGNMENT.finditer(text):
        fields[match[1]].append(setting_value(match[2]))
    if sum(map(len, fields.values())) != len(re.findall(
        r"\b(?:MARKETING_VERSION|CURRENT_PROJECT_VERSION)\s*=", text
    )):
        raise ValueError("Malformed Xcode version assignment")
    if any(not values for values in fields.values()):
        raise ValueError("Missing Xcode version settings")
    marketing = fields["MARKETING_VERSION"]
    if any(not re.fullmatch(VERSION, value) for value in marketing):
        raise ValueError("MARKETING_VERSION must be a strict literal X.Y.Z")
    version = marketing[0]
    for key, values in fields.items():
        for value in values:
            if value != version and not (
                key == "CURRENT_PROJECT_VERSION"
                and REFERENCES.get(value) == "MARKETING_VERSION"
            ):
                raise ValueError(f"Invalid or drifted {key}: {value!r}; expected {version}")
    data = plistlib.loads(PLIST.read_bytes())
    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
        if key == "CFBundleVersion" and key not in data:
            continue
        value = data.get(key)
        if not isinstance(value, str) or (
            value != version and value not in REFERENCES
        ):
            raise ValueError(f"Invalid or drifted {key}: {value!r}; expected {version}")
        if len(re.findall(r"<key>\s*" + key + r"\s*</key>", PLIST.read_text())) != 1:
            raise ValueError(f"Missing or duplicate plist key: {key}")
    return version


def bump(mode):
    current = source_version()
    parts = list(map(int, current.split(".")))
    index = ("major", "minor", "patch").index(mode)
    parts[index] += 1
    parts[index + 1:] = [0] * (2 - index)
    new = ".".join(map(str, parts))
    project = ASSIGNMENT.sub(
        lambda match: match[0] if setting_value(match[2]) in REFERENCES
        else match[0].replace(match[2], match[2].replace(current, new)),
        PROJECT.read_text(),
    )
    plist = PLIST.read_text()
    data = plistlib.loads(PLIST.read_bytes())
    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
        if key in data and data[key] not in REFERENCES:
            plist, count = re.subn(
                r"(<key>\s*" + key + r"\s*</key>\s*<string>)[^<]*(</string>)",
                lambda match: match[1] + new + match[2], plist,
            )
            if count != 1:
                raise ValueError(f"Cannot update {key}")
    PROJECT.write_text(project)
    PLIST.write_text(plist)
    if source_version() != new:
        raise ValueError("Post-bump validation failed")
    return new


def remote_ref(ref):
    result = run("git", "ls-remote", "--exit-code", "origin", ref, check=False)
    if result.returncode == 2 and not result.stdout.strip():
        return None
    if result.returncode:
        raise ValueError(f"Cannot query remote {ref}: {result.stderr}")
    rows = [line.split() for line in result.stdout.splitlines()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref:
        raise ValueError(f"Malformed remote ref response: {result.stdout!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", rows[0][0]):
        raise ValueError("Malformed remote SHA")
    return rows[0][0]


def bump_and_push(mode, branch, expected):
    validate_sha(expected)
    ref = f"refs/heads/{branch}"
    run("git", "check-ref-format", ref)
    if run("git", "rev-parse", "HEAD").stdout.strip() != expected:
        raise ValueError("Checkout differs from selected SHA")
    if run("git", "status", "--porcelain").stdout:
        raise ValueError("Bump requires a clean checkout")
    if remote_ref(ref) != expected:
        raise ValueError("Selected branch moved; dispatch Bump version again")
    version = bump(mode)
    run("git", "add", "--", *(str(path) for path in SOURCES))
    run("git", "commit", "-m", f"chore: bump version to {version}")
    # Check receive-pack's advertised old SHA, not only an earlier ls-remote.
    # The server then atomically rejects an intervening change to that old SHA.
    with tempfile.TemporaryDirectory() as directory:
        hook = Path(directory) / "pre-push"
        hook.write_text(
            "#!/bin/sh\nset -eu\n"
            "while read -r local_ref local_sha remote_ref remote_sha; do\n"
            '  if [ "$remote_ref" != "$BUMP_REF" ] || '
            '[ "$remote_sha" != "$BUMP_EXPECTED" ]; then\n'
            '    echo "Selected branch raced; refusing push" >&2\n'
            "    exit 1\n  fi\ndone\n"
        )
        hook.chmod(0o755)
        result = run(
            "git", "-c", "push.followTags=false", "-c", f"core.hooksPath={directory}",
            "push", "--porcelain", "--no-follow-tags", "origin", f"HEAD:{ref}",
            env={**os.environ, "BUMP_REF": ref, "BUMP_EXPECTED": expected},
        )
    require_push_status(result.stdout, " ", ref)
    return version


def api(method, endpoint, payload=None):
    args = ["gh", "api", "--method", method, endpoint]
    kwargs = {}
    if payload is not None:
        args += ["--input", "-"]
        kwargs["input"] = json.dumps(payload)
    result = run(*args, **kwargs)
    return json.loads(result.stdout)


def assert_absent(repository, tag):
    if remote_ref(f"refs/tags/{tag}") is not None:
        raise ValueError(f"Remote tag {tag} already exists")
    page = 1
    seen = set()
    while True:
        releases = api("GET", f"repos/{repository}/releases?per_page=100&page={page}")
        if not isinstance(releases, list) or len(releases) > 100:
            raise ValueError("Malformed releases page")
        for release in releases:
            if (
                not isinstance(release, dict)
                or type(release.get("id")) is not int
                or release["id"] <= 0
                or not isinstance(release.get("tag_name"), str)
                or type(release.get("draft")) is not bool
                or type(release.get("prerelease")) is not bool
                or release["id"] in seen
            ):
                raise ValueError("Malformed or repeated release record")
            seen.add(release["id"])
            if release["tag_name"] == tag:
                raise ValueError(f"Release {tag} already exists (including drafts)")
        if len(releases) < 100:
            return
        page += 1


def validate_sha(sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected an immutable 40-character commit SHA")


def require_push_status(output, status, ref):
    statuses = [line.split("\t") for line in output.splitlines() if "\t" in line]
    if len(statuses) != 1 or statuses[0][0] != status or (
        statuses[0][1].split(":")[-1] != ref
    ):
        raise ValueError("Ref was not newly updated by this run; refusing to continue")


def publish(repository, sha, assets):
    validate_sha(sha)
    if run("git", "rev-parse", "HEAD").stdout.strip() != sha:
        raise ValueError("Checkout differs from selected SHA")
    run("git", "diff", "--exit-code", "HEAD", "--", *(str(path) for path in SOURCES))
    tag = "v" + source_version()
    for asset in assets:
        if not Path(asset).is_file() or Path(asset).stat().st_size == 0:
            raise ValueError(f"Missing or empty asset: {asset}")
    assert_absent(repository, tag)
    # A same-SHA creation race reports '=' (up to date), not '*' (new).
    result = run(
        "git", "-c", "push.followTags=false", "push", "--porcelain",
        "--no-follow-tags", "origin", f"{sha}:refs/tags/{tag}",
    )
    require_push_status(result.stdout, "*", f"refs/tags/{tag}")
    if remote_ref(f"refs/tags/{tag}") != sha:
        raise ValueError("New remote tag does not point to the selected SHA")
    # Original softprops policy: empty body, no generated release notes.
    release = api("POST", f"repos/{repository}/releases", {
        "tag_name": tag, "target_commitish": sha, "name": tag,
        "draft": False, "prerelease": False, "body": "",
    })
    if (
        not isinstance(release, dict)
        or type(release.get("id")) is not int
        or release["id"] <= 0
        or release.get("tag_name") != tag
        or release.get("draft") is not False
        or release.get("prerelease") is not False
    ):
        raise ValueError("Malformed newly-created release response")
    print(f"Created release ID {release['id']} for {tag}", flush=True)
    run("gh", "release", "upload", tag, *assets, "--repo", repository)
    return tag


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version")
    for name in ("bump", "bump-push"):
        command = commands.add_parser(name)
        command.add_argument("mode", choices=("patch", "minor", "major"))
        if name == "bump-push":
            command.add_argument("branch")
            command.add_argument("sha")
    commands.add_parser("check")
    command = commands.add_parser("publish")
    command.add_argument("assets", nargs=2)
    args = parser.parse_args()
    try:
        if args.command == "version":
            print(source_version())
        elif args.command == "bump":
            print(bump(args.mode))
        elif args.command == "bump-push":
            print(bump_and_push(args.mode, args.branch, args.sha))
        elif args.command == "check":
            assert_absent(os.environ["GITHUB_REPOSITORY"], "v" + source_version())
        else:
            print(publish(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"], args.assets))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, file=sys.stderr)
        if args.command == "publish":
            print(
                "Publication stopped. Any created tag, release or assets are retained. "
                "Inspect remote state and follow README recovery guidance; do not retry blindly.",
                file=sys.stderr,
            )
        sys.exit(1)


if __name__ == "__main__":
    main()
