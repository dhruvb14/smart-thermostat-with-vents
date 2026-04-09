import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes, NavLink } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Rooms from "./pages/Rooms";
import Schedules from "./pages/Schedules";
import Settings from "./pages/Settings";
import Logs from "./pages/Logs";
import "./styles.css";

function Nav() {
  return (
    <nav className="nav">
      <div className="nav-brand">
        <span className="nav-icon">🌡</span>
        Flair Replacement
      </div>
      <div className="nav-links">
        <NavLink to="/" end>Dashboard</NavLink>
        <NavLink to="/rooms">Rooms</NavLink>
        <NavLink to="/schedules">Schedules</NavLink>
        <NavLink to="/settings">Settings</NavLink>
        <NavLink to="/logs">Logs</NavLink>
      </div>
    </nav>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Nav />
      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/rooms" element={<Rooms />} />
          <Route path="/schedules" element={<Schedules />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/logs" element={<Logs />} />
        </Routes>
      </main>
    </BrowserRouter>
  </React.StrictMode>
);
