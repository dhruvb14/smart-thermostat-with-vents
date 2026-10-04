#!/usr/bin/env python3
"""
Stages the goldens a visual-regression leg regenerated, for upload to the
`Commit updated goldens` fan-in.

Each leg owns a disjoint set of PNGs in e2e/screenshots/:

  auth   every golden named by a toHaveScreenshot() call in tests/auth.spec.ts
         (Fahrenheit only: the auth UI renders no temperature)
  F / C  every other golden for that unit

The auth set is read from the spec, not hardcoded. A hardcoded prefix list
(login-*, settings-menu-auth-*, mcp-tokens-card-*) let mcp-token-revoke-confirm
and settings-auth regenerate on every run without ever being staged, so they
were never committed and the leg stayed green while failing the same way each
time.

As a backstop, the script exits non-zero if the regenerate pass left any new or
modified golden in the working tree that this leg is not uploading. A golden
that would never be committed fails the job instead of passing silently.

Usage:
    python3 e2e/scripts/stage-goldens.py --leg F --dest /tmp/goldens
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent.parent
SCREENSHOTS = E2E_DIR / "screenshots"
AUTH_SPEC = E2E_DIR / "tests" / "auth.spec.ts"
UNIT_LABELS = {"F": "Fahrenheit", "C": "Celsius"}

# toHaveScreenshot("name.png", ...) — the call may wrap its argument onto
# the next line.
SCREENSHOT_CALL = re.compile(r'toHaveScreenshot\(\s*["\']([^"\']+)\.png["\']')


def auth_golden_names() -> set[str]:
    names = set(SCREENSHOT_CALL.findall(AUTH_SPEC.read_text()))
    if not names:
        sys.exit(f"No toHaveScreenshot() names found in {AUTH_SPEC}")
    return names


def owned_by_auth(filename: str, auth_names: set[str]) -> bool:
    # Goldens are named {arg}-{unit}-{project}.png (playwright.config.ts
    # snapshotPathTemplate). Match the full "{arg}-{unit}-" prefix so that
    # "login" claims login-Fahrenheit-* but not login-filled-Fahrenheit-*
    # (that one is claimed by its own "login-filled" entry).
    return any(filename.startswith(f"{n}-{u}-") for n in auth_names for u in UNIT_LABELS.values())


def leg_goldens(leg: str, auth_names: set[str]) -> list[Path]:
    if leg == "auth":
        unit = UNIT_LABELS["F"]
        return sorted(
            p for p in SCREENSHOTS.glob(f"*-{unit}-*.png") if owned_by_auth(p.name, auth_names)
        )
    unit = UNIT_LABELS[leg]
    return sorted(
        p for p in SCREENSHOTS.glob(f"*-{unit}-*.png") if not owned_by_auth(p.name, auth_names)
    )


def changed_goldens() -> set[str]:
    """New or modified PNGs under e2e/screenshots/ relative to the checkout."""
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", str(SCREENSHOTS)],
        check=True,
        capture_output=True,
        text=True,
        cwd=E2E_DIR,
    ).stdout
    return {Path(line[3:]).name for line in out.splitlines() if line.endswith(".png")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--leg", required=True, choices=["F", "C", "auth"])
    parser.add_argument("--dest", required=True, type=Path)
    args = parser.parse_args()

    auth_names = auth_golden_names()
    staged = leg_goldens(args.leg, auth_names)
    if not staged:
        sys.exit(f"No goldens found for leg {args.leg} in {SCREENSHOTS}")

    args.dest.mkdir(parents=True, exist_ok=True)
    for p in staged:
        shutil.copy2(p, args.dest / p.name)

    staged_names = {p.name for p in staged}
    changed = changed_goldens()
    print(f"Staged {len(staged)} goldens for leg {args.leg}; {len(changed)} new or modified:")
    for name in sorted(changed):
        print(f"  {name}")

    unstaged = sorted(changed - staged_names)
    if unstaged:
        print(
            f"::error::Leg {args.leg} regenerated goldens it does not upload, "
            "so they would never be committed:",
            file=sys.stderr,
        )
        for name in unstaged:
            print(f"  {name}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
