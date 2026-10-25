#!/usr/bin/env python3
"""Build the HACS release ZIP from an exact Git tag without publishing it."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def release_version(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Git tag to package, for example v0.2.7")
    parser.add_argument("output", type=Path, help="Path for the declared HACS ZIP asset")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    hacs = json.loads((root / "hacs.json").read_text(encoding="utf-8"))
    manifest_path = "custom_components/diesel_level_thermostat/manifest.json"
    expected_name = hacs["filename"]

    if args.output.name != expected_name:
        fail(f"output must be named {expected_name!r} to match hacs.json")

    tag_manifest = subprocess.run(
        ["git", "show", f"{args.tag}:{manifest_path}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if tag_manifest.returncode:
        fail(f"cannot read {manifest_path} from {args.tag}: {tag_manifest.stderr.strip()}")
    if json.loads(tag_manifest.stdout).get("version") != release_version(args.tag):
        fail(f"{args.tag} does not match the manifest version")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as output:
        archive = subprocess.run(
            ["git", "archive", "--format=zip", args.tag, "--", "custom_components/diesel_level_thermostat"],
            cwd=root,
            stdout=output,
        )
    if archive.returncode:
        fail(f"git archive failed for {args.tag}")

    verify = subprocess.run(
        [sys.executable, str(root / "scripts" / "verify_hacs_release_asset.py"), args.tag, str(args.output)],
        cwd=root,
    )
    if verify.returncode:
        raise SystemExit(verify.returncode)
    print(f"built {args.output}")


if __name__ == "__main__":
    main()
