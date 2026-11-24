# SPDX-License-Identifier: AGPL-3.0-or-later
"""
License parity: Plenum is AGPL-3.0-or-later, and every place that states the
license must agree.

1. The repo-root ``LICENSE`` is the verbatim GNU AGPL v3 text.
2. Every tracked source file carries an ``SPDX-License-Identifier`` header with
   the project identifier in its first lines, so a new file added without one
   fails CI instead of silently shipping with no license marker.
3. The package metadata (``pyproject.toml``, both ``package.json`` files and
   their lockfiles) and the Docker image label name the same identifier.

The header list is driven by ``git ls-files`` so build output, ``node_modules``
and other untracked files are never inspected.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SPDX_ID = "AGPL-3.0-or-later"
HEADER = f"SPDX-License-Identifier: {SPDX_ID}"

_REPO_ROOT = Path(__file__).parents[3]

# File kinds that take a comment header. JSON, YAML, Markdown and binary
# assets are covered by the repo-root LICENSE alone.
_HEADER_SUFFIXES = {".py", ".sh", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".css", ".html"}
_HEADER_NAMES = {"Dockerfile"}

# The header sits on line 1, or line 2 after a shebang / doctype.
_HEADER_WINDOW = 2


def _tracked_files() -> list[str]:
    if shutil.which("git") is None or not (_REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.splitlines()


def _needs_header(path: str) -> bool:
    p = Path(path)
    return p.suffix in _HEADER_SUFFIXES or p.name in _HEADER_NAMES


def test_license_file_is_agpl_v3() -> None:
    text = (_REPO_ROOT / "LICENSE").read_text()
    assert text.lstrip().startswith("GNU AFFERO GENERAL PUBLIC LICENSE")
    assert "Version 3, 19 November 2007" in text
    # The full text ends with the "How to Apply" appendix; a truncated copy
    # would not.
    assert "and how to apply and follow the GNU AGPL" in text


def test_every_source_file_has_spdx_header() -> None:
    missing = []
    for path in _tracked_files():
        if not _needs_header(path):
            continue
        with (_REPO_ROOT / path).open(encoding="utf-8") as fh:
            head = [fh.readline() for _ in range(_HEADER_WINDOW)]
        if not any(HEADER in line for line in head):
            missing.append(path)
    assert not missing, (
        f"These files need a '{HEADER}' comment on line 1 "
        f"(line 2 after a shebang or <!doctype html>):\n  " + "\n  ".join(missing)
    )


def test_pyproject_license() -> None:
    text = (_REPO_ROOT / "smart_vent" / "pyproject.toml").read_text()
    assert f'license = "{SPDX_ID}"' in text


@pytest.mark.parametrize("pkg_dir", ["smart_vent/frontend", "e2e"])
def test_package_json_license(pkg_dir: str) -> None:
    pkg = json.loads((_REPO_ROOT / pkg_dir / "package.json").read_text())
    lock = json.loads((_REPO_ROOT / pkg_dir / "package-lock.json").read_text())
    assert pkg["license"] == SPDX_ID
    assert lock["packages"][""]["license"] == SPDX_ID


def test_docker_image_label() -> None:
    text = (_REPO_ROOT / "smart_vent" / "Dockerfile").read_text()
    assert f'LABEL org.opencontainers.image.licenses="{SPDX_ID}"' in text
