#!/usr/bin/env python3
"""Verify the HACS ZIP locally, or verify the asset attached to a GitHub release."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def release_version(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def required_metadata(root: Path) -> tuple[str, str]:
    hacs = json.loads((root / "hacs.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (root / "custom_components" / "diesel_level_thermostat" / "manifest.json").read_text(encoding="utf-8")
    )
    return hacs["filename"], manifest["domain"]


def verify_zip(asset: Path, *, filename: str, domain: str, version: str) -> None:
    if asset.name != filename:
        fail(f"asset must be named {filename!r}, got {asset.name!r}")
    if not asset.is_file():
        fail(f"release asset is missing: {asset}")
    try:
        with zipfile.ZipFile(asset) as archive:
            if archive.testzip() is not None:
                fail(f"ZIP is corrupt: {asset}")
            manifest_name = "manifest.json"
            if manifest_name not in archive.namelist():
                fail(f"ZIP is missing {manifest_name}")
            if any(name.startswith("custom_components/") for name in archive.namelist()):
                fail("ZIP must contain the integration files at its root, not under custom_components/")
            packaged_manifest = json.loads(archive.read(manifest_name))
    except zipfile.BadZipFile:
        fail(f"not a ZIP file: {asset}")
    if packaged_manifest.get("domain") != domain:
        fail(f"ZIP manifest domain is {packaged_manifest.get('domain')!r}, expected {domain!r}")
    if packaged_manifest.get("version") != version:
        fail(f"ZIP manifest version is {packaged_manifest.get('version')!r}, expected {version!r}")


def verify_published(
    tag: str, filename: str, domain: str, version: str, repository: str
) -> None:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/tags/{tag}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "hacs-release-asset-check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            release = json.load(response)
    except Exception as error:
        fail(f"cannot read GitHub release {tag}: {error}")

    asset = next((item for item in release.get("assets", []) if item.get("name") == filename), None)
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
                )
    except Exception as error:
        fail(f"published asset cannot be downloaded: {error}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Release tag, for example v0.2.7")
    parser.add_argument("asset", nargs="?", type=Path, help="Local ZIP asset to validate")
    parser.add_argument("--published", action="store_true", help="Check the named GitHub release asset")
    parser.add_argument("--repository", default=None, help="owner/repository; defaults to GitHub Actions repository")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    filename, domain = required_metadata(root)
    version = release_version(args.tag)

    if args.asset is None and not args.published:
        fail("provide a local asset and/or --published")
    if args.asset is not None:
        verify_zip(args.asset, filename=filename, domain=domain, version=version)
    if args.published:
        repository = args.repository or __import__("os").environ.get("GITHUB_REPOSITORY")
        if not repository:
            fail("--published requires --repository or GITHUB_REPOSITORY")
        verify_published(args.tag, filename, domain, version, repository)

    print(f"release asset check passed for {args.tag}")


if __name__ == "__main__":
    main()
