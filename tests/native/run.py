#!/usr/bin/env python3
"""Compile actual native sources and the complete bundled SDK; never build a plugin."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[2]
BASELINE = "4f5a2240a95fea64d2ae578ee390236bfef02d50"
SDK_SHA256 = {
    "4DPluginAPI.c": "355ee208ad6c61dcc68e8432546fc42484888211dd07ed8fa3fdb4440f3edbd8",
    "4DPluginAPI.h": "7391841f0046f9e822b65ca6b07cafb3565aecfb58f65be4e0661adf97b8c786",
    "Flags.h": "89be5f9526b4f066543d93142760f2f7a630ddcdfb0540dfc20191c43563aa56",
    "PublicTypes.h": "acce13e0896a3f55a3d96bf849a42bdae7cbd5c6f92874f1f47e0e34ad070dcc",
    "PrivateTypes.h": "99e66ee558612e3fcc4efa11d8439aad65bdc982e4006e9122b4bfc1533bc6e7",
    "EntryPoints.h": "130b06373937b58ac6c5336a342fd291974371dd462d9c56c06cea4ffc8a717d",
}
CASES = (
    "picture-path", "picture-path-empty", "picture-bytes", "picture-copy",
    "picture-copy-empty", "picture-gray",
    "picture-size", "picture-thumbnail", "array", "scalar",
)


def build(source, destination):
    sdk = source / "4D Plugin API"
    for name, digest in SDK_SHA256.items():
        if hashlib.sha256((sdk / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"bundled SDK changed: {name}")
    sources = [sdk / "4DPluginAPI.c", *sorted((sdk / "Classes").glob("*.cpp")),
               source / "4DPlugin.cpp", ROOT / "tests/native/ownership.cpp"]
    subprocess.run([
        "xcrun", "clang++", "-std=c++11", "-x", "objective-c++",
        "-include", "Carbon/Carbon.h", "-I", str(sdk), "-I", str(sdk / "Classes"),
        "-Wno-deprecated-declarations", "-Wno-extern-c-compat",
        "-Wno-deprecated-register", "-g", "-O1",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        *map(str, sources), "-framework", "Cocoa", "-framework", "Carbon",
        "-framework", "DiscRecording", "-o", str(destination),
    ], check=True)


def run(binary, baseline=False):
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0",
               UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1")
    for case in CASES:
        for empty in ("allocated", "null"):
            result = subprocess.run([str(binary), case, empty], env=env,
                                    capture_output=True, text=True)
            expected_failure = baseline and case.startswith("picture-")
            # Null empty strings don't expose an initial empty-string leak,
            # but empty pictures are independent allocated reference owners.
            if expected_failure:
                marker = {
                    "picture-path": "duplicate or borrowed string disposal",
                    "picture-path-empty": ("duplicate or borrowed string disposal"
                                           if empty == "allocated"
                                           else "unbalanced picture references"),
                    "picture-copy": "AddressSanitizer: heap-buffer-overflow",
                    "picture-copy-empty": ("unbalanced string allocations"
                                           if empty == "allocated"
                                           else "unbalanced picture references"),
                }.get(case, "unbalanced picture references")
                if result.returncode == 0 or marker not in result.stderr:
                    print(result.stdout + result.stderr, file=sys.stderr)
                    raise RuntimeError(f"baseline did not fail as expected: {case}/{empty}")
                print(f"EXPECTED BASELINE FAILURE {case}/{empty}: {marker}")
            else:
                print(result.stdout, end="")
                if result.returncode:
                    print(result.stderr, file=sys.stderr)
                    raise RuntimeError(f"native regression failed: {case}/{empty}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true",
                        help="also prove the final harness fails the immutable baseline")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("this non-release harness requires macOS and Xcode command-line tools")
    with tempfile.TemporaryDirectory(prefix="devices-ownership-") as directory:
        temp = Path(directory)
        if args.baseline:
            archive = temp / "baseline.tar"
            with archive.open("wb") as output:
                subprocess.run(["git", "archive", BASELINE], cwd=ROOT, stdout=output, check=True)
            baseline = temp / "baseline"
            baseline.mkdir()
            with tarfile.open(archive) as source:
                source.extractall(baseline, filter="data")
            build(baseline, temp / "baseline-test")
            run(temp / "baseline-test", baseline=True)
        build(ROOT, temp / "fixed-test")
        run(temp / "fixed-test")


if __name__ == "__main__":
    main()
