"""Release guards reject incomplete or source-divergent integration archives."""

import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from scripts.verify_hacs_release_asset import (
    BRAND_ASSETS,
    RUNTIME_FILES,
    verify_brand_asset,
    verify_zip,
)

ROOT = Path(__file__).resolve().parents[1]


def files():
    component = ROOT / "custom_components/diesel_level_thermostat"
    return {
        str(p.relative_to(component)): p.read_bytes()
        for p in component.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }


def archive(tmp_path, contents):
    asset = tmp_path / "diesel-level-thermostat.zip"
    with zipfile.ZipFile(asset, "w") as z:
        for name, content in contents.items():
            z.writestr(name, content)
    return asset


def verify(asset, expected=None):
    verify_zip(
        asset,
        filename=asset.name,
        domain="diesel_level_thermostat",
        version="0.2.10",
        expected_files=expected,
    )


def test_actual_component_archive_matches_source(tmp_path):
    contents = files()
    verify(archive(tmp_path, contents), contents)


@pytest.mark.parametrize("missing", sorted(RUNTIME_FILES | set(BRAND_ASSETS)))
def test_missing_runtime_or_brand_file_is_rejected(tmp_path, missing):
    contents = files()
    contents.pop(missing)
    with pytest.raises(SystemExit):
        verify(archive(tmp_path, contents))


def test_modified_python_is_rejected_against_exact_source(tmp_path):
    expected = files()
    contents = dict(expected)
    contents["control.py"] += b"\n# changed\n"
    with pytest.raises(SystemExit):
        verify(archive(tmp_path, contents), expected)


def test_invalid_python_is_rejected(tmp_path):
    contents = files()
    contents["climate.py"] = b"this is invalid python !"
    with pytest.raises(SystemExit):
        verify(archive(tmp_path, contents))


@pytest.mark.parametrize("name", ["../escape.py", "/absolute.py", "custom_components/nested.py"])
def test_unsafe_or_nested_paths_are_rejected(tmp_path, name):
    contents = files()
    contents[name] = b"pass"
    with pytest.raises(SystemExit):
        verify(archive(tmp_path, contents))


def test_truncated_png_header_is_rejected():
    content = (ROOT / "custom_components/diesel_level_thermostat/brand/icon.png").read_bytes()[:29]
    with pytest.raises(SystemExit):
        verify_brand_asset("icon.png", content, (256, 256))


def test_opaque_png_is_rejected():
    out = io.BytesIO()
    Image.new("RGBA", (256, 256), (1, 2, 3, 255)).save(out, format="PNG")
    with pytest.raises(SystemExit):
        verify_brand_asset("icon.png", out.getvalue(), (256, 256))


def test_builder_uses_tag_metadata_even_when_working_hacs_changes(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init")
    component = root / "custom_components/diesel_level_thermostat"
    component.mkdir(parents=True)
    for name, data in files().items():
        p = component / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (root / "hacs.json").write_text(json.dumps({"filename": "diesel-level-thermostat.zip"}))
    (root / "scripts").mkdir()
    for name in ["build_hacs_release_asset.py", "verify_hacs_release_asset.py"]:
        shutil.copyfile(ROOT / "scripts" / name, root / "scripts" / name)
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    git("tag", "v0.2.10")
    (root / "hacs.json").write_text(json.dumps({"filename": "different.zip"}))
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/build_hacs_release_asset.py"),
            "v0.2.10",
            str(root / "dist/diesel-level-thermostat.zip"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    verify(root / "dist/diesel-level-thermostat.zip")
