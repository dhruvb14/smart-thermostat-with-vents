"""
Parity test: every option defined in config.yaml must be read via
bashio::config in run.sh.

This catches the class of bug where a new add-on option is added to
config.yaml but the shell entry-point is never updated to read and
export it, so the user's setting silently has no effect at runtime.

The beta pointer add-on (smart_vent_beta/config.yaml) shares the same
run.sh, so its options block must stay in lockstep with the stable one;
that is asserted separately below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[3]
_CONFIG_YAML = _REPO_ROOT / "smart_vent" / "config.yaml"
_CONFIG_YAML_BETA = _REPO_ROOT / "smart_vent_beta" / "config.yaml"
_RUN_SH = _REPO_ROOT / "smart_vent" / "run.sh"


def _block_lines(config_yaml: Path, key: str) -> list[str]:
    """Return the raw lines of a top-level block (``options:`` / ``schema:``)
    from a config.yaml, without PyYAML (which is not a project dependency)."""
    lines: list[str] = []
    inside = False
    for line in config_yaml.read_text().splitlines():
        if line.rstrip() == f"{key}:":
            inside = True
            continue
        if inside:
            if line and not line.startswith(" "):
                break  # reached the next top-level key
            lines.append(line.rstrip())
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _option_keys(config_yaml: Path) -> list[str]:
    """Extract keys from the 'options:' block in a config.yaml without PyYAML."""
    keys: list[str] = []
    for line in _block_lines(config_yaml, "options"):
        m = re.match(r"^  (\w+):", line)
        if m:
            keys.append(m.group(1))
    return keys


class TestAddonConfigParity:
    def test_every_option_is_read_by_run_sh(self):
        """Each key under 'options' in config.yaml must have a
        bashio::config '<key>' call or get_config helper call in run.sh,
        otherwise the user's add-on setting is never loaded into the
        process environment."""
        run_sh = _RUN_SH.read_text()
        # Check for either literal bashio::config calls OR get_config helper usage
        missing = [
            key
            for key in _option_keys(_CONFIG_YAML)
            if f"bashio::config '{key}'" not in run_sh and f"get_config '{key}'" not in run_sh
        ]
        assert not missing, (
            f"Option(s) defined in config.yaml but never read in run.sh "
            f"via bashio::config or get_config: {missing}. "
            f"Add a line like: VAR=$(get_config '{missing[0]}' 'default_value')"
        )

    def test_beta_options_match_stable(self):
        """The beta pointer add-on (smart_vent_beta/config.yaml) runs the same
        image and the same run.sh as stable, so its 'options' block must stay
        identical to stable's. If they drift, a beta option would silently go
        unread by run.sh (which only the stable parity test above guards)."""
        stable = _option_keys(_CONFIG_YAML)
        beta = _option_keys(_CONFIG_YAML_BETA)
        assert beta == stable, (
            "smart_vent_beta/config.yaml options drifted from smart_vent/config.yaml. "
            f"stable={stable} beta={beta}. Keep the beta manifest's options/schema "
            "block a verbatim copy of stable's (only name/slug/image/version/ports differ)."
        )

    @pytest.mark.parametrize("block", ["options", "schema"])
    def test_beta_blocks_are_verbatim_copies(self, block: str):
        """ "Verbatim copy" means the values and the types too, not just the key
        names: a beta DEFAULT that drifts (``require_auth: false``) or a beta
        SCHEMA entry that drifts (``int?`` vs ``int``) would sail past the
        key-set check above while changing how the beta add-on behaves."""
        stable = _block_lines(_CONFIG_YAML, block)
        beta = _block_lines(_CONFIG_YAML_BETA, block)
        assert stable, f"no '{block}:' block found in {_CONFIG_YAML} — parser drift"
        assert beta == stable, (
            f"smart_vent_beta/config.yaml '{block}' block drifted from stable's.\n"
            f"stable:\n  " + "\n  ".join(stable) + "\nbeta:\n  " + "\n  ".join(beta)
        )
