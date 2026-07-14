"""Unit tests for MCP token scope classification (Issue #373, backend/scopes.py)."""

from __future__ import annotations

from backend import scopes


class TestRequiredScope:
    def test_get_is_read(self):
        assert scopes.required_scope("GET", "/api/rooms") == scopes.READ
        assert scopes.required_scope("get", "/api/system/status") == scopes.READ

    def test_write_verbs_are_write(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert scopes.required_scope(method, "/api/rooms") == scopes.WRITE

    def test_backup_is_destructive_despite_being_a_get(self):
        # /api/backup streams the whole DB — destructive even though it's a GET.
        assert scopes.required_scope("GET", "/api/backup") == scopes.DESTRUCTIVE

    def test_restore_and_restart_are_destructive(self):
        assert scopes.required_scope("POST", "/api/restore") == scopes.DESTRUCTIVE
        assert scopes.required_scope("POST", "/api/restart") == scopes.DESTRUCTIVE

    def test_token_management_is_destructive(self):
        assert scopes.required_scope("GET", "/api/mcp/tokens") == scopes.DESTRUCTIVE
        assert scopes.required_scope("POST", "/api/mcp/tokens") == scopes.DESTRUCTIVE
        assert scopes.required_scope("DELETE", "/api/mcp/tokens/abc123") == scopes.DESTRUCTIVE


class TestScopeSatisfies:
    def test_ranking(self):
        # A higher scope satisfies lower requirements.
        assert scopes.scope_satisfies(scopes.DESTRUCTIVE, scopes.READ)
        assert scopes.scope_satisfies(scopes.DESTRUCTIVE, scopes.WRITE)
        assert scopes.scope_satisfies(scopes.DESTRUCTIVE, scopes.DESTRUCTIVE)
        assert scopes.scope_satisfies(scopes.WRITE, scopes.READ)
        assert scopes.scope_satisfies(scopes.WRITE, scopes.WRITE)
        assert scopes.scope_satisfies(scopes.READ, scopes.READ)

    def test_insufficient(self):
        assert not scopes.scope_satisfies(scopes.READ, scopes.WRITE)
        assert not scopes.scope_satisfies(scopes.READ, scopes.DESTRUCTIVE)
        assert not scopes.scope_satisfies(scopes.WRITE, scopes.DESTRUCTIVE)

    def test_unknown_scopes_fail_closed(self):
        assert not scopes.scope_satisfies("admin", scopes.READ)
        assert not scopes.scope_satisfies(scopes.DESTRUCTIVE, "nonsense")
        assert not scopes.scope_satisfies("", scopes.READ)
