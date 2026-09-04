"""Parity test: every temperature field lives in three places in lockstep.

The smart-vent codebase has three independent declarations of "which keys
are temperatures":

  1. `smart_vent/backend/api/routes.py` -- TEMPERATURE_FIELDS dict, the
     authoritative Python registry. Backend handlers convert these via
     `_to_f` / `_delta_to_f` at the write boundary.

  2. `e2e/tests/temperature-fields.ts` -- TEMPERATURE_FIELDS array, the
     authoritative TypeScript manifest. The e2e round-trip suite drives
     off this list.

  3. `e2e/tests/temperature-units.spec.ts` -- per-test `// @covers:`
     markers naming which fields each round-trip test exercises.

Drift between any two of these is the exact class of bug that produced
issue #231 (frontend converted twice; tests passed in isolation because
each side asserted a different contract). This test fails CI loudly the
moment any of:

  - A field is added to the Python registry but missing from the TS
    manifest (or vice versa).
  - A field's `kind` disagrees between Python and TS.
  - A `ui: true` TS entry has no `// @covers:` mention in the spec.
  - A `// @covers:` tag references a field that isn't in the manifest.

Running this in CI is much cheaper than the e2e matrix and catches the
"forgot to add a round-trip for the new field" mistake at the unit level.
"""

import re
from pathlib import Path

from backend.api.routes import TEMPERATURE_FIELDS as BACKEND_FIELDS

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_TS = REPO_ROOT / "e2e" / "tests" / "temperature-fields.ts"
SPEC_TS = REPO_ROOT / "e2e" / "tests" / "temperature-units.spec.ts"
ROUTES_PY = REPO_ROOT / "smart_vent" / "backend" / "api" / "routes.py"

# Matches one object literal entry inside the TEMPERATURE_FIELDS array.
# Tolerant of any property order and arbitrary `endpoints` arrays.
_ENTRY_RE = re.compile(
    r"\{\s*"
    r"field:\s*\"(?P<field>[^\"]+)\",\s*"
    r"kind:\s*\"(?P<kind>absolute_nullable|absolute|delta_nullable|delta)\",\s*"
    r"ui:\s*(?P<ui>true|false),\s*"
    r"endpoints:\s*\[[^\]]*\],?\s*"
    r"\}",
    re.MULTILINE,
)

# Match `// @covers: a, b` only when it's the first non-whitespace on a line,
# so JSDoc references like `\`// @covers:\` line` inside the top-of-file
# comment don't get scraped as actual markers.
_COVERS_RE = re.compile(r"^[ \t]*//\s*@covers:\s*([^\n]+)$", re.MULTILINE)


def _parse_ts_manifest() -> list[dict]:
    text = MANIFEST_TS.read_text()
    entries = [m.groupdict() for m in _ENTRY_RE.finditer(text)]
    assert entries, (
        f"No entries parsed from {MANIFEST_TS} — the regex in this test is "
        f"likely out of sync with the manifest format. Check whether a new "
        f"property was added to TempField."
    )
    return entries


def _parse_covers_markers() -> set[str]:
    """Return the union of fields named in every `// @covers: …` line."""
    fields: set[str] = set()
    for match in _COVERS_RE.finditer(SPEC_TS.read_text()):
        for item in match.group(1).split(","):
            name = item.strip()
            if name:
                fields.add(name)
    return fields


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_python_and_ts_manifests_have_identical_field_sets():
    """The exact set of field names must match between the Python registry
    and the TypeScript manifest. Adding to one but not the other breaks the
    contract that the e2e tests rely on."""
    py_fields = set(BACKEND_FIELDS.keys())
    ts_entries = _parse_ts_manifest()
    ts_fields = {e["field"] for e in ts_entries}

    py_only = py_fields - ts_fields
    ts_only = ts_fields - py_fields
    assert not py_only and not ts_only, (
        "Temperature field registry drift:\n"
        f"  - in routes.py only:                {sorted(py_only) or 'none'}\n"
        f"  - in temperature-fields.ts only:    {sorted(ts_only) or 'none'}\n"
        "Add the missing entries to whichever manifest is incomplete."
    )


def test_python_and_ts_kinds_agree():
    """For every shared field, the conversion kind must match. A mismatched
    kind silently corrupts data: `_to_f` strips 32 °F, `_delta_to_f` doesn't.
    """
    ts_kinds = {e["field"]: e["kind"] for e in _parse_ts_manifest()}
    mismatches = [
        (name, BACKEND_FIELDS[name], ts_kinds[name])
        for name in BACKEND_FIELDS
        if name in ts_kinds and BACKEND_FIELDS[name] != ts_kinds[name]
    ]
    assert not mismatches, (
        "Temperature field kind mismatch between Python and TS manifests:\n"
        + "\n".join(f"  - {name}: routes.py={py}, ts={ts}" for name, py, ts in mismatches)
    )


def test_every_ui_field_has_an_e2e_covers_marker():
    """Each `ui: true` entry in the TS manifest must be mentioned in a
    `// @covers:` line inside temperature-units.spec.ts. Adding a UI write
    path for a new field without a round-trip test is exactly how the #231
    double-conversion shipped to production."""
    ts_entries = _parse_ts_manifest()
    required = {e["field"] for e in ts_entries if e["ui"] == "true"}
    covered = _parse_covers_markers()
    missing = required - covered
    assert not missing, (
        "UI-writable fields with no e2e @covers marker:\n"
        f"  {sorted(missing)}\n\n"
        "Add a `// @covers: <field>[, <field>]` line to a round-trip test "
        "in e2e/tests/temperature-units.spec.ts."
    )


def test_no_orphan_covers_markers():
    """A `// @covers:` marker for a field that isn't in the manifest means
    the spec is documenting coverage of something that doesn't exist (or
    has been renamed). Either fix the manifest or fix the marker."""
    ts_entries = _parse_ts_manifest()
    known = {e["field"] for e in ts_entries}
    covered = _parse_covers_markers()
    orphans = covered - known
    assert not orphans, (
        "@covers markers referencing unknown fields:\n"
        f"  {sorted(orphans)}\n\n"
        "Add these to temperature-fields.ts or fix the typo."
    )


# The registry literal itself, so it can be excluded before searching for
# real handler references (see the test below).
_REGISTRY_BLOCK_RE = re.compile(
    r"^TEMPERATURE_FIELDS: dict\[str, str\] = \{.*?^\}\n", re.MULTILINE | re.DOTALL
)


def test_every_registered_field_appears_in_routes_source():
    """Sanity check: a field declared in TEMPERATURE_FIELDS must actually be
    referenced somewhere in routes.py OUTSIDE the registry — otherwise the
    registry has a dead entry that won't be reached by any handler.

    Excluding the registry literal is what makes this falsifiable: every key
    is a string inside that dict by construction, so searching the whole file
    matched itself and the assertion could never fail.
    """
    src = ROUTES_PY.read_text()
    block = _REGISTRY_BLOCK_RE.search(src)
    assert block, (
        "Could not locate the TEMPERATURE_FIELDS literal in routes.py — this "
        "test's regex has drifted from the declaration."
    )
    handlers = (src[: block.start()] + src[block.end() :]).replace('"', "'")
    missing = [f for f in BACKEND_FIELDS if f not in handlers]
    # `f not in handlers` is over-broad — a field name could appear in an
    # unrelated comment — but that's the conservative direction (we want to
    # flag *absence*, not presence).
    assert not missing, (
        f"TEMPERATURE_FIELDS keys never referenced by a routes.py handler "
        f"(dead registry entries): {missing}"
    )
