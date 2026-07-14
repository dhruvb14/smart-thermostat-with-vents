import { useState } from "react";
import { useMcp, useAuth } from "../contexts";

/**
 * MCP server enable/disable + setup guidance (Settings page).
 *
 * This used to be an item in the ⚙️ gear dropdown whose confirm modal crammed
 * the whole setup guide into one wall of bold text (issue #471). The toggle now
 * lives here, right next to the token-minting card it relates to, and the setup
 * steps render as a readable definition list that is always visible — so the
 * confirm modal can stay short.
 *
 * Shown regardless of `require_auth` (MCP can run unauthenticated); the token
 * card next to it is the part gated on auth.
 */
export default function McpServerCard() {
  const { mcpEnabled, toggleMcp } = useMcp();
  const { requireAuth } = useAuth();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const confirm = () => {
    void toggleMcp();
    setConfirmOpen(false);
  };
  const cancel = () => setConfirmOpen(false);

  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <div className="card-title">MCP server</div>

      <div className="mcp-server-status">
        <span
          className="system-toggle-dot"
          style={{ background: mcpEnabled ? "var(--green)" : "var(--red)" }}
        />
        <span className="mcp-server-state">{mcpEnabled ? "Running" : "Off"}</span>
        <button
          className={`btn ${mcpEnabled ? "btn-secondary" : "btn-primary"}`}
          onClick={() => setConfirmOpen(true)}
        >
          {mcpEnabled ? "Turn off" : "Turn on"}
        </button>
      </div>

      <div className="form-hint" style={{ marginTop: ".75rem" }}>
        Lets an MCP client (e.g. Claude) attach to Plenum to manage rooms, schedules, thermostats
        and more.
      </div>

      <dl className="mcp-setup">
        <dt>Separate port</dt>
        <dd>
          The MCP server listens on its own port (default <code>9099</code>) — not this web
          UI&apos;s port — and that port must be published to reach it.
        </dd>

        <dt>Home Assistant OS / Supervised</dt>
        <dd>
          HAOS doesn&apos;t allow direct Docker port access, so publish the port from the add-on:
          open the Plenum add-on → <em>Configuration</em> tab → <em>Network</em> section, set a host
          port for <code>9099/tcp</code>, then Save and Restart. No <code>9099/tcp</code> row?
          Update the add-on to the latest version — HA re-reads the ports when the add-on version
          changes, so a normal Update surfaces it (no reinstall needed).
        </dd>

        <dt>Docker (standalone)</dt>
        <dd>
          Publish the container port, e.g. <code>-p 9099:9099</code> (or a <code>ports:</code> entry
          in Compose).
        </dd>

        <dt>Connect</dt>
        <dd>
          Point your MCP client at <code>http://&lt;host&gt;:9099/mcp</code>.
        </dd>

        <dt>Authentication</dt>
        <dd>
          {requireAuth ? (
            <>
              Required — mint a bearer token below and set an{" "}
              <code>Authorization: Bearer &lt;token&gt;</code> header on your MCP client.
            </>
          ) : (
            <>The endpoint is unauthenticated — only expose the port on a trusted network.</>
          )}
        </dd>
      </dl>

      {confirmOpen && (
        <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && cancel()}>
          <div className="modal">
            <div className="modal-title">
              {mcpEnabled ? "Turn off MCP server?" : "Turn on MCP server?"}
            </div>
            {mcpEnabled ? (
              <p>
                The MCP endpoint stops accepting connections. Existing tokens are kept and work
                again when you re-enable it.
              </p>
            ) : (
              <>
                <p>
                  Plenum will start the MCP server on its own port (default <code>9099</code>).
                </p>
                <p>
                  You still need to publish that port and point a client at it — the steps are
                  listed on this page under <strong>MCP server</strong>.
                </p>
                {requireAuth ? (
                  <p>Authentication is on, so mint a bearer token after enabling.</p>
                ) : (
                  <p>
                    Authentication is off, so the endpoint is open — only expose it on a trusted
                    network.
                  </p>
                )}
              </>
            )}
            <div className="modal-footer">
              <button className="btn btn-secondary" onClick={cancel}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={confirm}>
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
