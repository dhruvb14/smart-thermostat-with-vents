import React, { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Route, Routes, NavLink } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Rooms from "./pages/Rooms";
import Schedules from "./pages/Schedules";
import Thermostats from "./pages/Thermostats";
import Logs from "./pages/Logs";
// Lazy-loaded so recharts (~400 KB) only ships when /metrics is opened.
// (Issue #85 Phase 5e — keeps the dashboard / rooms / etc. pages snappy.)
const Metrics = lazy(() => import("./pages/Metrics"));
import DevMode from "./pages/DevMode";
import {
  getSystemStatus,
  setSystemEnabled,
  setDevModeApi,
  setMcpEnabled,
  connectWS,
  getSettings,
} from "./api";
import {
  SystemContext,
  DevModeContext,
  McpContext,
  UnitContext,
  buildUnitContext,
  useSystem,
  useDevMode,
  useMcp,
} from "./contexts";
import UnitChangeBanner from "./components/UnitChangeBanner";
import VacationModeBanner from "./components/VacationModeBanner";

function AppRoot({ children }: { children: React.ReactNode }) {
  const [enabled, setEnabled] = useState<boolean>(true);
  const [toggling, setToggling] = useState(false);
  const [devMode, setDevMode] = useState<boolean>(false);
  const [togglingDev, setTogglingDev] = useState(false);
  const [mcpEnabled, setMcpEnabledState] = useState<boolean>(false);
  const [togglingMcp, setTogglingMcp] = useState(false);
  const [unit, setUnit] = useState<"F" | "C">("F");

  // Seed from API on mount
  useEffect(() => {
    getSystemStatus()
      .then((s) => {
        setEnabled(s.enabled);
        setDevMode(s.dev_mode ?? false);
        setMcpEnabledState(s.mcp_enabled ?? false);
      })
      .catch(() => {});
    getSettings()
      .then((s) => setUnit(s.temperature_unit))
      .catch(() => {});
  }, []);

  // Subscribe to real-time updates
  useEffect(() => {
    const cleanup = connectWS((event) => {
      if (event.type === "system_enabled_changed") {
        setEnabled(event.data.enabled as boolean);
      }
      if (event.type === "dev_mode_changed") {
        setDevMode(event.data.dev_mode as boolean);
      }
      if (event.type === "mcp_enabled_changed") {
        setMcpEnabledState(event.data.mcp_enabled as boolean);
      }
    });
    return cleanup;
  }, []);

  const toggle = async () => {
    if (toggling) return;
    setToggling(true);
    try {
      const result = await setSystemEnabled(!enabled);
      setEnabled(result.enabled);
    } catch {
      // ignore
    } finally {
      setToggling(false);
    }
  };

  const toggleDevMode = async () => {
    if (togglingDev) return;
    setTogglingDev(true);
    try {
      const result = await setDevModeApi(!devMode);
      setDevMode(result.dev_mode);
    } catch {
      // ignore
    } finally {
      setTogglingDev(false);
    }
  };

  const toggleMcp = async () => {
    if (togglingMcp) return;
    setTogglingMcp(true);
    try {
      const result = await setMcpEnabled(!mcpEnabled);
      setMcpEnabledState(result.mcp_enabled);
    } catch {
      // ignore
    } finally {
      setTogglingMcp(false);
    }
  };

  // Memoize so the context object (and its toDisplay/toDisplayDelta function
  // identities) stays stable across AppRoot re-renders — toggling System
  // On/Off or Dev Mode re-renders AppRoot, and a fresh context every time would
  // reset every Thermostat card's in-progress form edits. (Issue #293)
  const unitContextValue = useMemo(() => buildUnitContext(unit), [unit]);

  return (
    <SystemContext.Provider value={{ enabled, toggle }}>
      <DevModeContext.Provider value={{ devMode, toggleDevMode }}>
        <McpContext.Provider value={{ mcpEnabled, toggleMcp }}>
          <UnitContext.Provider value={unitContextValue}>{children}</UnitContext.Provider>
        </McpContext.Provider>
      </DevModeContext.Provider>
    </SystemContext.Provider>
  );
}

// ---------------------------------------------------------------------------
// Nav + SettingsDropdown
// ---------------------------------------------------------------------------

type ConfirmKind = "system" | "devmode" | "mcp";

function SettingsDropdown() {
  const { enabled, toggle } = useSystem();
  const { devMode, toggleDevMode } = useDevMode();
  const { mcpEnabled, toggleMcp } = useMcp();
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState<ConfirmKind | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const handleSystemClick = () => {
    setOpen(false);
    setConfirm("system");
  };

  const handleDevModeClick = () => {
    setOpen(false);
    setConfirm("devmode");
  };

  const handleMcpClick = () => {
    setOpen(false);
    setConfirm("mcp");
  };

  const handleConfirm = () => {
    if (confirm === "system") toggle();
    else if (confirm === "devmode") toggleDevMode();
    else if (confirm === "mcp") toggleMcp();
    setConfirm(null);
  };

  const handleCancel = () => setConfirm(null);

  return (
    <div className="settings-dropdown" ref={ref}>
      <button
        className="settings-gear-btn"
        onClick={() => setOpen((o) => !o)}
        aria-label="Settings"
      >
        ⚙️
      </button>

      {open && (
        <div className="settings-menu">
          <button className="settings-menu-item" onClick={handleSystemClick}>
            <span
              className="system-toggle-dot"
              style={{ background: enabled ? "var(--green)" : "var(--red)" }}
            />
            {enabled ? "System On" : "System Off"}
          </button>
          <button className="settings-menu-item" onClick={handleDevModeClick}>
            🛠 {devMode ? "Dev On" : "Dev Off"}
          </button>
          <button className="settings-menu-item" onClick={handleMcpClick}>
            <span
              className="system-toggle-dot"
              style={{ background: mcpEnabled ? "var(--green)" : "var(--red)" }}
            />
            {mcpEnabled ? "MCP On" : "MCP Off"}
          </button>
          <a
            className="settings-menu-item"
            href="api/docs/"
            onClick={(e) => {
              e.preventDefault();
              setOpen(false);
              window.location.href = "api/docs/";
            }}
          >
            📖 API Docs
          </a>
        </div>
      )}

      {confirm === "system" && (
        <div
          className="modal-backdrop"
          onClick={(e) => e.target === e.currentTarget && handleCancel()}
        >
          <div className="modal">
            <div className="modal-title">{enabled ? "Turn off system?" : "Turn on system?"}</div>
            <p>
              {enabled
                ? "When system is disabled, all active cycles are terminated and all vents open."
                : "When system is enabled, control will resume and cycles will restart based on current settings."}
            </p>
            <div className="modal-footer">
              <button className="btn btn-secondary" onClick={handleCancel}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={handleConfirm}>
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}

      {confirm === "devmode" && (
        <div
          className="modal-backdrop"
          onClick={(e) => e.target === e.currentTarget && handleCancel()}
        >
          <div className="modal">
            <div className="modal-title">Developer Mode</div>
            <p>
              {devMode
                ? "When disabled, the system will resume normal operation and control thermostats and vents directly."
                : "When enabled, the system runs but logs all actions instead of actually controlling thermostats and vents. No Home Assistant changes will be made. This is useful for testing and simulation."}
            </p>
            <div className="modal-footer">
              <button className="btn btn-secondary" onClick={handleCancel}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={handleConfirm}>
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}

      {confirm === "mcp" && (
        <div
          className="modal-backdrop"
          onClick={(e) => e.target === e.currentTarget && handleCancel()}
        >
          <div className="modal">
            <div className="modal-title">
              {mcpEnabled ? "Turn off MCP server?" : "Turn on MCP server?"}
            </div>
            {mcpEnabled ? (
              <p>When disabled, the MCP endpoint stops accepting connections.</p>
            ) : (
              <>
                <p>
                  When enabled, an MCP client (e.g. Claude) can attach to this add-on to manage
                  rooms, schedules, thermostats and more.
                </p>
                <p>
                  <strong>
                    The MCP server runs on its own separate port (default 9099) — not this web
                    UI&apos;s port — so you must expose that port before any client can reach it.
                  </strong>
                </p>
                <p>
                  <strong>Home Assistant OS / Supervised:</strong> HAOS doesn&apos;t allow direct
                  Docker port access, so publish the port from the add-on itself — open the Plenum
                  add-on → <em>Configuration</em> tab → <em>Network</em> section → set a host port
                  for <code>9099/tcp</code> → Save, then Restart the add-on.
                </p>
                <p>
                  <strong>Docker (standalone):</strong> publish the container port, e.g.{" "}
                  <code>-p 9099:9099</code> (or a <code>ports:</code> entry in Compose).
                </p>
                <p>
                  Then attach your client at <code>http://&lt;host&gt;:9099/mcp</code>. The endpoint
                  is unauthenticated — only expose the port on a trusted network.
                </p>
              </>
            )}
            <div className="modal-footer">
              <button className="btn btn-secondary" onClick={handleCancel}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={handleConfirm}>
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Nav() {
  const [open, setOpen] = useState(false);
  const { devMode } = useDevMode();
  const close = () => setOpen(false);

  return (
    <nav className="nav">
      {/* Always-visible top row */}
      <NavLink to="/" end className="nav-brand" style={{ textDecoration: "none" }}>
        <span className="nav-icon">🌡</span>
        Plenum
        <span
          className="nav-version"
          style={{
            fontSize: ".65rem",
            opacity: 0.45,
            marginLeft: ".4rem",
            fontWeight: 400,
            letterSpacing: 0,
          }}
        >
          v{import.meta.env.VITE_APP_VERSION ?? "dev"}
        </span>
      </NavLink>

      {/* Desktop links */}
      <div className="nav-links nav-links-desktop">
        <NavLink to="/" end onClick={close}>
          Dashboard
        </NavLink>
        <NavLink to="/rooms" onClick={close}>
          Rooms
        </NavLink>
        <NavLink to="/schedules" onClick={close}>
          Schedules
        </NavLink>
        <NavLink to="/thermostats" onClick={close}>
          Thermostats
        </NavLink>
        <NavLink to="/metrics" onClick={close}>
          Metrics
        </NavLink>
        <NavLink to="/logs" onClick={close}>
          Logs
        </NavLink>
        {devMode && (
          <NavLink to="/dev" onClick={close} className="nav-dev-link">
            🛠 Dev Mode
          </NavLink>
        )}
      </div>

      <div className="nav-right">
        <SettingsDropdown />
        {/* Hamburger — mobile only */}
        <button
          className="nav-hamburger"
          onClick={() => setOpen((o) => !o)}
          aria-label="Toggle menu"
        >
          <span className={`hamburger-icon ${open ? "open" : ""}`} />
        </button>
      </div>

      {/* Mobile dropdown */}
      {open && (
        <div className="nav-mobile-menu">
          <NavLink to="/" end onClick={close}>
            Dashboard
          </NavLink>
          <NavLink to="/rooms" onClick={close}>
            Rooms
          </NavLink>
          <NavLink to="/schedules" onClick={close}>
            Schedules
          </NavLink>
          <NavLink to="/thermostats" onClick={close}>
            Thermostats
          </NavLink>
          <NavLink to="/metrics" onClick={close}>
            Metrics
          </NavLink>
          <NavLink to="/logs" onClick={close}>
            Logs
          </NavLink>
          {devMode && (
            <NavLink to="/dev" onClick={close}>
              🛠 Dev Mode
            </NavLink>
          )}
        </div>
      )}
    </nav>
  );
}

export default function App() {
  return (
    <AppRoot>
      <Nav />
      <UnitChangeBanner />
      <main className="main">
        {/* Inside .main so it gets the same gutter (1.5rem) and top gap as the
            page content — i.e. 1:1 with the StaleSensorsBanner card — while
            still rendering above the page title. */}
        <VacationModeBanner />
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/rooms" element={<Rooms />} />
          <Route path="/schedules" element={<Schedules />} />
          <Route path="/thermostats" element={<Thermostats />} />
          <Route
            path="/metrics"
            element={
              <Suspense
                fallback={
                  <div className="loading">
                    <div className="spinner" /> Loading metrics…
                  </div>
                }
              >
                <Metrics />
              </Suspense>
            }
          />
          <Route path="/logs" element={<Logs />} />
          <Route path="/dev" element={<DevMode />} />
        </Routes>
      </main>
    </AppRoot>
  );
}
