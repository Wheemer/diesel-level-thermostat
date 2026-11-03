"""Verify the HACS ZIP locally, or verify the asset attached to a GitHub release."""

from __future__ import annotations

import argparse
import ast
import io
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from urllib.error import URLError

from PIL import Image, UnidentifiedImageError

BRAND_ASSETS = {
    "brand/icon.png": (256, 256),
    "brand/icon@2x.png": (512, 512),
    "brand/logo.png": (256, 256),
    "brand/logo@2x.png": (512, 512),
    "brand/dark_icon.png": (256, 256),
    "brand/dark_icon@2x.png": (512, 512),
    "brand/dark_logo.png": (256, 256),
    "brand/dark_logo@2x.png": (512, 512),
}


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def release_version(tag: str) -> str:
    return tag.removeprefix("v")


RUNTIME_FILES = {
    "__init__.py",
    "climate.py",
    "config_flow.py",
    "const.py",
    "control.py",
    "source.py",
    "strings.json",
    "translations/en.json",
}
COMPONENT = "custom_components/diesel_level_thermostat"


def git_file(root: Path, ref: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"], cwd=root, capture_output=True, check=True
    ).stdout


def required_metadata(root: Path, ref: str) -> tuple[str, str, str]:
    hacs = json.loads(git_file(root, ref, "hacs.json"))
    manifest = json.loads(git_file(root, ref, f"{COMPONENT}/manifest.json"))
    return hacs["filename"], manifest["domain"], manifest["version"]


def source_files(root: Path, ref: str) -> dict[str, bytes]:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, "--", COMPONENT],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        name.removeprefix(f"{COMPONENT}/"): git_file(root, ref, name)
        for name in result.stdout.splitlines()
    }


def verify_brand_asset(name: str, content: bytes, expected_size: tuple[int, int]) -> None:
    """Require a transparent RGBA PNG with the expected dimensions."""
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format != "PNG" or image.size != expected_size or image.mode != "RGBA":
                fail(f"brand asset {name} must be a transparent RGBA PNG sized {expected_size}")
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            if image.getchannel("A").getextrema()[0] == 255:
                fail(f"brand asset {name} has no transparency")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        fail(f"brand asset {name} is invalid: {error}")


def verify_zip(
    asset: Path,
    *,
    filename: str,
    domain: str,
    version: str,
    expected_files: dict[str, bytes] | None = None,
) -> None:
    if asset.name != filename:
        fail(f"asset must be named {filename!r}, got {asset.name!r}")
    if not asset.is_file():
        fail(f"release asset is missing: {asset}")
    try:
        with zipfile.ZipFile(asset) as archive:
            if archive.testzip() is not None:
                fail(f"ZIP is corrupt: {asset}")
            names = archive.namelist()
            if len(names) != len(set(names)) or any(
                name.startswith("/") or ".." in Path(name).parts or "\\" in name for name in names
            ):
                fail("ZIP contains duplicate or unsafe paths")
            missing = RUNTIME_FILES - set(names)
            if missing:
                fail(f"ZIP is missing runtime files: {sorted(missing)}")
            for name in names:
                if name.endswith(".py"):
                    try:
                        ast.parse(archive.read(name), filename=name)
                    except (SyntaxError, UnicodeError) as error:
                        fail(f"ZIP Python file is invalid: {name}: {error}")
            if expected_files is not None:
                if set(names) != set(expected_files) or any(
                    archive.read(name) != content for name, content in expected_files.items()
                ):
                    fail("ZIP contents differ from the exact source ref")
            manifest_name = "manifest.json"
            if manifest_name not in archive.namelist():
                fail(f"ZIP is missing {manifest_name}")
            if any(name.startswith("custom_components/") for name in archive.namelist()):
                fail("ZIP must contain integration files at its root, not under custom_components/")
            packaged_manifest = json.loads(archive.read(manifest_name))
            for name, expected_size in BRAND_ASSETS.items():
                if name not in archive.namelist():
                    fail(f"ZIP is missing required brand asset: {name}")
                verify_brand_asset(name, archive.read(name), expected_size)
    except zipfile.BadZipFile:
        fail(f"not a ZIP file: {asset}")
    if packaged_manifest.get("domain") != domain:
        fail(f"ZIP manifest domain is {packaged_manifest.get('domain')!r}, expected {domain!r}")
    if packaged_manifest.get("version") != version:
        fail(f"ZIP manifest version is {packaged_manifest.get('version')!r}, expected {version!r}")


def verify_published(
    tag: str,
    filename: str,
    domain: str,
    version: str,
    repository: str,
    expected_files: dict[str, bytes],
) -> None:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/tags/{tag}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "hacs-release-asset-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            release = json.load(response)
    except (URLError, OSError, ValueError, TimeoutError) as error:
        fail(f"cannot read GitHub release {tag}: {error}")

    asset = next(
        (item for item in release.get("assets", []) if item.get("name") == filename),
        None,
    )
    if asset is None:
        fail(f"published release {tag} is missing {filename}")

    download = urllib.request.Request(asset["browser_download_url"])
    try:
        with urllib.request.urlopen(download, timeout=20) as response:
            if response.status != 200:
                fail(f"published asset returned HTTP {response.status}")
            with tempfile.TemporaryDirectory() as directory:
                downloaded_asset = Path(directory) / filename
                with downloaded_asset.open("wb") as output:
                    shutil.copyfileobj(response, output)
                verify_zip(
                    downloaded_asset,
                    filename=filename,
                    domain=domain,
                    version=version,
                    expected_files=expected_files,
                )
    except (URLError, OSError, ValueError, TimeoutError) as error:
        fail(f"published asset cannot be downloaded: {error}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Release tag, for example v0.2.7")
    parser.add_argument("asset", nargs="?", type=Path, help="Local ZIP asset to validate")
    parser.add_argument(
        "--published", action="store_true", help="Check the named GitHub release asset"
    )
    parser.add_argument(
        "--repository",
        default=None,
        help="owner/repository; defaults to GitHub Actions repository",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    filename, domain, version = required_metadata(root, args.tag)
    if args.tag.startswith("v") and release_version(args.tag) != version:
        fail("release tag and manifest version do not match")
    expected_files = source_files(root, args.tag)

    if args.asset is None and not args.published:
        fail("provide a local asset and/or --published")
    if args.asset is not None:
        verify_zip(
            args.asset,
            filename=filename,
            domain=domain,
            version=version,
            expected_files=expected_files,
        )
    if args.published:
        repository = args.repository or __import__("os").environ.get("GITHUB_REPOSITORY")
        if not repository:
            fail("--published requires --repository or GITHUB_REPOSITORY")
        verify_published(args.tag, filename, domain, version, repository, expected_files)

    print(f"release asset check passed for {args.tag}")


if __name__ == "__main__":
    main()
