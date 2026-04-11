import React, { createContext, useContext, useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes, NavLink } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Rooms from "./pages/Rooms";
import Schedules from "./pages/Schedules";
import Thermostats from "./pages/Thermostats";
import Logs from "./pages/Logs";
import { getSystemStatus, setSystemEnabled, connectWS } from "./api";
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

function AppRoot({ children }: { children: React.ReactNode }) {
  const [enabled, setEnabled] = useState<boolean>(true);
  const [toggling, setToggling] = useState(false);

  // Seed from API on mount
  useEffect(() => {
    getSystemStatus().then(s => setEnabled(s.enabled)).catch(() => {});
  }, []);

  // Subscribe to real-time updates
  useEffect(() => {
    const cleanup = connectWS((event) => {
      if (event.type === "system_enabled_changed") {
        setEnabled(event.data.enabled as boolean);
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

  return (
    <SystemContext.Provider value={{ enabled, toggle }}>
      {children}
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

function Nav() {
  const [open, setOpen] = useState(false);
  const close = () => setOpen(false);

  return (
    <nav className="nav">
      {/* Always-visible top row */}
      <div className="nav-brand">
        <span className="nav-icon">🌡</span>
        Flair Replacement
      </div>

      {/* Desktop links */}
      <div className="nav-links nav-links-desktop">
        <NavLink to="/" end onClick={close}>Dashboard</NavLink>
        <NavLink to="/rooms" onClick={close}>Rooms</NavLink>
        <NavLink to="/schedules" onClick={close}>Schedules</NavLink>
        <NavLink to="/thermostats" onClick={close}>Thermostats</NavLink>
        <NavLink to="/logs" onClick={close}>Logs</NavLink>
      </div>

      <div className="nav-right">
        <SystemToggle />
        {/* Hamburger — mobile only */}
        <button
          className="nav-hamburger"
          onClick={() => setOpen(o => !o)}
          aria-label="Toggle menu"
        >
          <span className={`hamburger-icon ${open ? "open" : ""}`} />
        </button>
      </div>

      {/* Mobile dropdown */}
      {open && (
        <div className="nav-mobile-menu">
          <NavLink to="/" end onClick={close}>Dashboard</NavLink>
          <NavLink to="/rooms" onClick={close}>Rooms</NavLink>
          <NavLink to="/schedules" onClick={close}>Schedules</NavLink>
          <NavLink to="/thermostats" onClick={close}>Thermostats</NavLink>
          <NavLink to="/logs" onClick={close}>Logs</NavLink>
        </div>
      )}
    </nav>
  );
}

// ---------------------------------------------------------------------------
// Root render
// ---------------------------------------------------------------------------

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <AppRoot>
        <Nav />
        <main className="main">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/rooms" element={<Rooms />} />
            <Route path="/schedules" element={<Schedules />} />
            <Route path="/thermostats" element={<Thermostats />} />
            <Route path="/logs" element={<Logs />} />
          </Routes>
        </main>
      </AppRoot>
    </BrowserRouter>
  </React.StrictMode>
);
