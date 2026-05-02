"""
Parity test: every option defined in config.yaml must be read via
bashio::config in run.sh.

This catches the class of bug where a new add-on option is added to
config.yaml but the shell entry-point is never updated to read and
export it, so the user's setting silently has no effect at runtime.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).parents[3]
_CONFIG_YAML = _REPO_ROOT / "smart_vent" / "config.yaml"
_RUN_SH = _REPO_ROOT / "smart_vent" / "run.sh"


def _option_keys() -> list[str]:
    cfg = yaml.safe_load(_CONFIG_YAML.read_text())
    return list(cfg.get("options", {}).keys())


def _run_sh_text() -> str:
    return _RUN_SH.read_text()


class TestAddonConfigParity:
    def test_every_option_is_read_by_run_sh(self):
        """Each key under 'options' in config.yaml must have a
        bashio::config '<key>' call in run.sh, otherwise the user's
        add-on setting is never loaded into the process environment."""
        run_sh = _run_sh_text()
        missing = [
            key for key in _option_keys()
            if f"bashio::config '{key}'" not in run_sh
        ]
        assert not missing, (
            f"Option(s) defined in config.yaml but never read in run.sh "
            f"via bashio::config: {missing}. "
            f"Add a line like: VAR=$(bashio::config '{missing[0]}' 2>/dev/null || echo '')"
        )
