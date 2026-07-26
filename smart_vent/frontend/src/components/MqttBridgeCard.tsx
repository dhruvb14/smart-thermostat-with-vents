import { useCallback, useEffect, useState } from "react";
import { getMqttStatus, setMqttEnabled, type MqttStatus } from "../api";
import { Frozen } from "../ci";

/**
 * MQTT bridge on/off + resolved-configuration readout (Settings page, #519).
 *
 * Two different kinds of setting live side by side here, and the split is
 * deliberate:
 *
 * - **Enabled** is a runtime toggle stored in the DB, exactly like the MCP
 *   server's. It is the user's switch, so it belongs in the UI.
 * - **Everything else** — broker host/port/credentials, topic prefix, discovery
 *   — is deployment configuration (add-on options / env vars) resolved once at
 *   boot, in the same class as `require_auth` and the OIDC settings. Making it
 *   editable here would put a second source of truth against `config.yaml`, so
 *   it is reported read-only instead. The topic prefix especially needs
 *   reporting: it is derived from the add-on slug and is otherwise invisible.
 */
export default function MqttBridgeCard() {
  const [status, setStatus] = useState<MqttStatus | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    getMqttStatus()
      .then(setStatus)
      .catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    // The connection state changes without any user action (a broker restart,
    // a network blip), so poll while the page is open.
    const timer = setInterval(refresh, 10_000);
    return () => clearInterval(timer);
  }, [refresh]);

  const toggle = async () => {
    if (!status) return;
    setBusy(true);
    try {
      await setMqttEnabled(!status.enabled);
      refresh();
    } finally {
      setBusy(false);
    }
  };

  if (!status) return null;

  const running = status.enabled && status.connected;
  const dotColor = running ? "var(--green)" : status.enabled ? "var(--orange)" : "var(--red)";
  const stateLabel = !status.enabled ? "Off" : status.connected ? "Connected" : "Connecting…";

  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <div className="card-title">MQTT bridge</div>

      <div className="mcp-server-status">
        <span className="system-toggle-dot" style={{ background: dotColor }} />
        {/* The connect/disconnect state flips on its own, so freeze it under CI
            or the visual-regression goldens could never stabilise. */}
        <span className="mcp-server-state">
          <Frozen>{stateLabel}</Frozen>
        </span>
        <button
          className={`btn ${status.enabled ? "btn-secondary" : "btn-primary"}`}
          onClick={toggle}
          disabled={busy || !status.configured}
          // The Settings page has more than one "Turn on" button, so name this
          // one for screen readers instead of leaving it ambiguous.
          aria-label={status.enabled ? "Turn off MQTT bridge" : "Turn on MQTT bridge"}
        >
          {status.enabled ? "Turn off" : "Turn on"}
        </button>
      </div>

      <div className="form-hint" style={{ marginTop: ".75rem" }}>
        Lets Home Assistant automations control Plenum directly — enable or disable a schedule, hold
        a room, clear presence, flip vacation mode. Controls appear as real HA entities, so they are
        usable in the automation UI with no YAML.
      </div>

      {!status.configured && (
        <div className="form-hint" style={{ color: "var(--orange)", marginTop: ".5rem" }}>
          No broker is configured, so the bridge cannot be turned on. Set <code>mqtt_enabled</code>{" "}
          in the add-on <em>Configuration</em> tab — on Home Assistant OS the broker is discovered
          from the built-in MQTT service automatically, and for standalone Docker also set{" "}
          <code>mqtt_host</code>.
        </div>
      )}

      {status.configured && (
        <dl className="mcp-setup">
          <dt>Broker</dt>
          <dd>
            <code>
              {status.host}:{status.port}
            </code>
          </dd>

          <dt>Topic prefix</dt>
          <dd>
            Every topic starts with <code>{status.topic_prefix}/</code> — for example{" "}
            <code>{status.topic_prefix}/room/&lt;room&gt;/hold/set</code>. Rooms are addressable by
            their id or by their name.
            {status.prefix_is_fallback && (
              <div style={{ color: "var(--orange)", marginTop: ".35rem" }}>
                This is the default prefix, not one derived from an add-on slug (there is no
                Supervisor here). Two Plenum containers on the same broker would collide — set{" "}
                <code>mqtt_topic_prefix</code> on at least one of them.
              </div>
            )}
          </dd>

          <dt>Discovery</dt>
          <dd>
            {status.discovery ? (
              <>
                On — controls are published under <code>{status.discovery_prefix}/</code> and show
                up as Home Assistant entities automatically.
              </>
            ) : (
              <>
                Off — no entities are created. Automations can still publish to the command topics
                directly.
              </>
            )}
          </dd>

          <dt>Access</dt>
          <dd>
            MQTT is gated by your broker&apos;s own ACLs, not by Plenum&apos;s login. Equipment
            safety settings (short-cycle protection, airflow floor, cycle timeout) are deliberately
            not exposed over MQTT.
          </dd>

          {status.last_error && !status.connected && (
            <>
              <dt>Last error</dt>
              <dd style={{ color: "var(--red)" }}>
                <Frozen>{status.last_error}</Frozen>
              </dd>
            </>
          )}
        </dl>
      )}
    </div>
  );
}
