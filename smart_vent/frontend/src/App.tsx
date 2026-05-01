import React, { lazy, Suspense, useEffect, useMemo, useState } from "react";
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
import { getSystemStatus, setSystemEnabled, setDevModeApi, connectWS, getSettings } from "./api";
import {
  SystemContext,
  DevModeContext,
  UnitContext,
  useSystem,
  useDevMode,
  type UnitContextValue,
} from "./contexts";
import UnitChangeBanner from "./components/UnitChangeBanner";

function AppRoot({ children }: { children: React.ReactNode }) {
  const [enabled, setEnabled] = useState<boolean>(true);
  const [toggling, setToggling] = useState(false);
  const [devMode, setDevMode] = useState<boolean>(false);
  const [togglingDev, setTogglingDev] = useState(false);
  const [unit, setUnit] = useState<"F" | "C">("F");

  // Seed from API on mount
  useEffect(() => {
    getSystemStatus()
      .then((s) => {
        setEnabled(s.enabled);
        setDevMode(s.dev_mode ?? false);
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

  const unitContextValue = useMemo<UnitContextValue>(() => {
    const isCelsius = unit === "C";
    const toDisplay = isCelsius
      ? (f: number) => parseFloat(((f - 32) * (5 / 9)).toFixed(1))
      : (f: number) => f;
    const toStorage = isCelsius
      ? (c: number) => parseFloat((c * (9 / 5) + 32).toFixed(2))
      : (f: number) => f;
    const fmtTemp = (f: number) => `${toDisplay(f).toFixed(1)}${isCelsius ? "°C" : "°F"}`;
    return { unit, isCelsius, toDisplay, toStorage, fmtTemp, unitLabel: isCelsius ? "°C" : "°F" };
  }, [unit]);

  return (
    <SystemContext.Provider value={{ enabled, toggle }}>
      <DevModeContext.Provider value={{ devMode, toggleDevMode }}>
        <UnitContext.Provider value={unitContextValue}>{children}</UnitContext.Provider>
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
