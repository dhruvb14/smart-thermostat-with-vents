"""Parity test: every event-log category the backend emits has a Logs-page filter.

The backend tags each structured event with a free-text ``category`` (via
``routes.emit`` or an ``EventLogger.log`` call). The Logs page's Live Feed
offers a dropdown to isolate one category at a time, driven by the
``CATEGORIES`` array in ``smart_vent/frontend/src/pages/Logs.tsx``.

Nothing linked the two, so ``auth`` — added by the #373 auth work, and the
category covering failed/successful direct-port logins, OIDC sign-in, and MCP
token issue/revoke — was emitted, stored, and streamed, but could not be
filtered: it only ever appeared under "all" (Issue #580). That is the class of
bug this test closes, and it is also the CLAUDE.md rule "every backend/API
feature must have a UI control" applied to a list rather than a form field.

The backend call sites are the source of truth: this test discovers them by
AST-walking ``backend/`` and asserts the TypeScript array names exactly the
categories found, plus the "all" pass-through. Adding a category to an
``emit()`` call without adding a chip fails here, cheaply, at the unit level.
"""

import ast
import re
from pathlib import Path

SMART_VENT = Path(__file__).resolve().parents[2]
BACKEND_DIR = SMART_VENT / "backend"
LOGS_TSX = SMART_VENT / "frontend" / "src" / "pages" / "Logs.tsx"

# The dropdown's pass-through entry; not a backend category.
ALL = "all"

# Positional index of the `category` argument for each recognised call shape.
#   routes.emit(request, level, category, message, details=None)
#   <…>logger.log(level, category, message, details=None)
_EMIT_CATEGORY_ARG = 2
_LOG_CATEGORY_ARG = 1

# Function names whose *bodies* forward a caller-supplied category rather than
# naming one (``routes.emit`` and ``EventLogger.log`` themselves). Calls inside
# them are plumbing, not call sites.
_FORWARDING_FUNCS = {"emit", "log"}

_ARRAY_RE = re.compile(r"const CATEGORIES\s*=\s*\[(?P<body>.*?)\]\s*;", re.DOTALL)
_STRING_RE = re.compile(r'"([^"]+)"')


def _forwarding_spans(tree: ast.Module) -> list[tuple[int, int]]:
    """Line ranges of the helpers that merely relay a `category` parameter."""
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name in _FORWARDING_FUNCS
        ):
            spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _category_arg_index(call: ast.Call) -> int | None:
    """Which positional argument of `call` is the event category, if any."""
    func = call.func
    if isinstance(func, ast.Name) and func.id == "emit":
        return _EMIT_CATEGORY_ARG
    if isinstance(func, ast.Attribute) and func.attr == "log":
        receiver = func.value
        name = receiver.attr if isinstance(receiver, ast.Attribute) else getattr(receiver, "id", "")
        # `log.info(...)`/`log.exception(...)` are the stdlib logger; the event
        # logger is always bound to a name ending in "logger"
        # (`logger`, `event_logger`, `_event_logger`, `_dev_logger`, `_logger`).
        if name.endswith("logger"):
            return _LOG_CATEGORY_ARG
    return None


def _scan_backend() -> tuple[set[str], list[str]]:
    """Return (literal categories emitted, call sites whose category isn't literal)."""
    categories: set[str] = set()
    dynamic: list[str] = []
    for path in sorted(BACKEND_DIR.rglob("*.py")):
        if "tests" in path.relative_to(BACKEND_DIR).parts:
            continue
        tree = ast.parse(path.read_text())
        spans = _forwarding_spans(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            index = _category_arg_index(node)
            if index is None:
                continue
            if any(start <= node.lineno <= end for start, end in spans):
                continue
            where = f"{path.relative_to(SMART_VENT)}:{node.lineno}"
            if len(node.args) <= index:
                dynamic.append(f"{where} (category passed by keyword)")
                continue
            arg = node.args[index]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                categories.add(arg.value)
            else:
                dynamic.append(where)
    return categories, dynamic


def _frontend_categories() -> list[str]:
    match = _ARRAY_RE.search(LOGS_TSX.read_text())
    assert match, (
        f"Could not find the CATEGORIES array in {LOGS_TSX} — this test's regex "
        f"is out of sync with the file. Check whether the array was renamed or "
        f"restructured."
    )
    return _STRING_RE.findall(match.group("body"))


class TestEventLogCategoryParity:
    def test_backend_scan_finds_the_known_call_sites(self):
        """Guard the scanner itself: a matcher that silently matches nothing
        would make every assertion below vacuously true."""
        categories, _ = _scan_backend()
        assert len(categories) >= 5, (
            f"Only found {sorted(categories)} — the AST matcher in this test has "
            f"probably drifted from how events are emitted."
        )
        # Spot-check the two ends of the range: the oldest category and the one
        # whose absence from the UI list was Issue #580.
        assert {"system", "auth"} <= categories

    def test_every_emitted_category_is_filterable(self):
        backend, _ = _scan_backend()
        frontend = set(_frontend_categories()) - {ALL}
        missing_chip = sorted(backend - frontend)
        orphan_chip = sorted(frontend - backend)
        assert not missing_chip, (
            f"{LOGS_TSX.name}'s CATEGORIES is missing {missing_chip}. The backend "
            f"emits these, so a user can only see them under '{ALL}'. Add them to "
            f"the array (Issue #580)."
        )
        assert not orphan_chip, (
            f"{LOGS_TSX.name}'s CATEGORIES offers {orphan_chip}, which nothing in "
            f"backend/ emits — the filter would always come back empty. Remove "
            f"them, or add the emit() call they were meant for."
        )

    def test_all_passthrough_is_first(self):
        frontend = _frontend_categories()
        assert frontend[0] == ALL, (
            f"'{ALL}' must lead CATEGORIES — the Live Feed's default state is the "
            f"unfiltered view and the select renders in array order."
        )

    def test_no_duplicate_chips(self):
        frontend = _frontend_categories()
        assert len(frontend) == len(set(frontend)), (
            f"Duplicate entries in CATEGORIES: {frontend}. React would warn on the "
            f"repeated <option> key."
        )

    def test_no_category_escapes_the_scan(self):
        """A category built at runtime (an f-string, a variable, a keyword arg)
        can't be checked against the UI list, so this test would stop being a
        guarantee. If you need one, extend the scanner deliberately."""
        _, dynamic = _scan_backend()
        assert not dynamic, (
            f"These event-log call sites don't pass a literal category, so this "
            f"parity test cannot see them: {dynamic}"
        )
