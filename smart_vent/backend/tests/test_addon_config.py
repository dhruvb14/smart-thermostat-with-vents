"""
Parity test: every option defined in config.yaml must be read via
bashio::config in run.sh.

This catches the class of bug where a new add-on option is added to
config.yaml but the shell entry-point is never updated to read and
export it, so the user's setting silently has no effect at runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[3]
_CONFIG_YAML = _REPO_ROOT / "smart_vent" / "config.yaml"
_RUN_SH = _REPO_ROOT / "smart_vent" / "run.sh"


def _option_keys() -> list[str]:
    """Extract keys from the 'options:' block in config.yaml without PyYAML."""
    keys: list[str] = []
    in_options = False
    for line in _CONFIG_YAML.read_text().splitlines():
        if line.rstrip() == "options:":
            in_options = True
            continue
        if in_options:
            if line and not line.startswith(" "):
                break  # reached the next top-level key
            m = re.match(r"^  (\w+):", line)
            if m:
                keys.append(m.group(1))
    return keys


class TestAddonConfigParity:
    def test_every_option_is_read_by_run_sh(self):
        """Each key under 'options' in config.yaml must have a
        bashio::config '<key>' call in run.sh, otherwise the user's
        add-on setting is never loaded into the process environment."""
        run_sh = _RUN_SH.read_text()
        missing = [key for key in _option_keys() if f"bashio::config '{key}'" not in run_sh]
        assert not missing, (
            f"Option(s) defined in config.yaml but never read in run.sh "
            f"via bashio::config: {missing}. "
            f"Add a line like: VAR=$(bashio::config '{missing[0]}' 2>/dev/null || echo '')"
        )
