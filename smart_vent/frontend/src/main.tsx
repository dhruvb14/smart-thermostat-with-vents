import React, { createContext, useContext, useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes, NavLink } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Rooms from "./pages/Rooms";
import Schedules from "./pages/Schedules";
import Thermostats from "./pages/Thermostats";
import Logs from "./pages/Logs";
import DevMode from "./pages/DevMode";
import { getSystemStatus, setSystemEnabled, setDevModeApi, connectWS } from "./api";
import "./styles.css";

// ---------------------------------------------------------------------------
// System context
// ---------------------------------------------------------------------------

interface SystemContextValue {
  enabled: boolean;
  toggle: () => Promise<void>;
}

const SystemContext = createContext<SystemContextValue>({
  enabled: true,
  toggle: async () => {},
});

export function useSystem() {
  return useContext(SystemContext);
}

// ---------------------------------------------------------------------------
// Developer mode context
// ---------------------------------------------------------------------------

interface DevModeContextValue {
  devMode: boolean;
  toggleDevMode: () => Promise<void>;
}

const DevModeContext = createContext<DevModeContextValue>({
  devMode: false,
  toggleDevMode: async () => {},
});

export function useDevMode() {
  return useContext(DevModeContext);
}

function AppRoot({ children }: { children: React.ReactNode }) {
  const [enabled, setEnabled] = useState<boolean>(true);
  const [toggling, setToggling] = useState(false);
  const [devMode, setDevMode] = useState<boolean>(false);
  const [togglingDev, setTogglingDev] = useState(false);

  // Seed from API on mount
  useEffect(() => {
    getSystemStatus()
      .then((s) => {
        setEnabled(s.enabled);
        setDevMode(s.dev_mode ?? false);
      })
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

  return (
    <SystemContext.Provider value={{ enabled, toggle }}>
      <DevModeContext.Provider value={{ devMode, toggleDevMode }}>
        {children}
      </DevModeContext.Provider>
    </SystemContext.Provider>
  );
}

// ---------------------------------------------------------------------------
// Nav + SystemToggle
// ---------------------------------------------------------------------------

function SystemToggle() {
  const { enabled, toggle } = useSystem();
  return (
    <button
      className={`system-toggle ${enabled ? "enabled" : "disabled"}`}
      onClick={toggle}
      title={enabled ? "System enabled — click to disable" : "System disabled — click to enable"}
    >
      <span className="system-toggle-dot" />
      {enabled ? "System On" : "System Off"}
    </button>
  );
}

function DevModeToggle() {
  const { devMode, toggleDevMode } = useDevMode();
  return (
    <button
      className={`dev-mode-toggle ${devMode ? "active" : ""}`}
      onClick={toggleDevMode}
      title={
        devMode
          ? "Developer mode ON — engine runs but no HA changes. Click to disable."
          : "Click to enable developer mode"
      }
    >
      🛠 {devMode ? "Dev On" : "Dev Off"}
    </button>
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
        <DevModeToggle />
        <SystemToggle />
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

// ---------------------------------------------------------------------------
// Root render
// ---------------------------------------------------------------------------

const _ingressMatch = location.pathname.match(/^(\/api\/hassio_ingress\/[^/]+)/);
const _ingressBasename = _ingressMatch ? _ingressMatch[1] : "";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter basename={_ingressBasename}>
      <AppRoot>
        <Nav />
        <main className="main">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/rooms" element={<Rooms />} />
            <Route path="/schedules" element={<Schedules />} />
            <Route path="/thermostats" element={<Thermostats />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/dev" element={<DevMode />} />
          </Routes>
        </main>
      </AppRoot>
    </BrowserRouter>
  </React.StrictMode>
);
