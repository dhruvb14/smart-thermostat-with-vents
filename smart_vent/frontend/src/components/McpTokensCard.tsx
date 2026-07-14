import React, { useEffect, useState } from "react";
import { listMcpTokens, mintMcpToken, revokeMcpToken, type McpToken, type McpScope } from "../api";

const SCOPE_HELP: Record<McpScope, string> = {
  read: "Read-only: list and inspect rooms, schedules, metrics, logs.",
  write: "Read + write: also create/update rooms, schedules, thermostats.",
  destructive: "Full access: also restart, restore, download backups, manage tokens.",
};

/**
 * MCP bearer-token management (#373 Phase 4). Rendered only when `require_auth`
 * is on (the parent gates it) — with auth off, MCP is open and tokens are
 * pointless. Mints a token (secret shown once), lists tokens, and revokes them.
 */
export default function McpTokensCard() {
  const [tokens, setTokens] = useState<McpToken[]>([]);
  const [label, setLabel] = useState("");
  const [scope, setScope] = useState<McpScope>("read");
  const [minting, setMinting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [freshSecret, setFreshSecret] = useState<string | null>(null);

  const load = () => {
    listMcpTokens()
      .then(setTokens)
      .catch(() => setTokens([]));
  };
  useEffect(load, []);

  const mint = async (e: React.FormEvent) => {
    e.preventDefault();
    if (minting || !label.trim()) return;
    setMinting(true);
    setError(null);
    try {
      const created = await mintMcpToken(label.trim(), scope);
      setFreshSecret(created.token);
      setLabel("");
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to mint token");
    } finally {
      setMinting(false);
    }
  };

  const revoke = async (id: string) => {
    try {
      await revokeMcpToken(id);
      load();
    } catch {
      // ignore — the list will simply not change
    }
  };

  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <div className="card-title">MCP access tokens</div>
      <div className="form-hint" style={{ marginBottom: ".75rem" }}>
        When authentication is required, MCP clients must present a bearer token. Mint one below,
        copy the secret (shown only once), and configure your MCP client with an{" "}
        <code>Authorization: Bearer &lt;token&gt;</code> header.
      </div>

      <form onSubmit={mint} className="mcp-token-form">
        <div className="form-group">
          <label className="form-label" htmlFor="mcp-token-label">
            Label
          </label>
          <input
            id="mcp-token-label"
            className="form-control"
            placeholder="e.g. Claude Desktop"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
        </div>
        <div className="form-group">
          <label className="form-label" htmlFor="mcp-token-scope">
            Scope
          </label>
          <select
            id="mcp-token-scope"
            className="form-control"
            value={scope}
            onChange={(e) => setScope(e.target.value as McpScope)}
          >
            <option value="read">read</option>
            <option value="write">write</option>
            <option value="destructive">destructive</option>
          </select>
          <div className="form-hint">{SCOPE_HELP[scope]}</div>
        </div>
        <button className="btn btn-primary" type="submit" disabled={minting || !label.trim()}>
          {minting ? "Minting…" : "Mint token"}
        </button>
        {error && (
          <span className="text-danger" style={{ marginLeft: ".5rem" }}>
            {error}
          </span>
        )}
      </form>

      {freshSecret && (
        <div className="mcp-token-secret" role="status">
          <strong>Copy this token now — it won&apos;t be shown again:</strong>
          <code className="mcp-token-value">{freshSecret}</code>
          <button className="btn btn-secondary btn-sm" onClick={() => setFreshSecret(null)}>
            Done
          </button>
        </div>
      )}

      {tokens.length === 0 ? (
        <div className="text-sm text-muted" style={{ marginTop: ".75rem" }}>
          No tokens yet.
        </div>
      ) : (
        <ul className="mcp-token-list">
          {tokens.map((t) => (
            <li key={t.id} className="mcp-token-item">
              <div>
                <span className="mcp-token-item-label">{t.label}</span>{" "}
                <span className={`badge badge-blue`}>{t.scope}</span>
                <div className="text-sm text-muted">
                  Last used: {t.last_used_at ? t.last_used_at : "never"}
                </div>
              </div>
              <button className="btn btn-danger btn-sm" onClick={() => void revoke(t.id)}>
                Revoke
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
