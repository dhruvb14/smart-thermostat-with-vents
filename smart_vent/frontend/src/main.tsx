import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router";
import App from "./App";
import { installIosZoomGuard } from "./iosZoomGuard";
import "./styles.css";

// Stop iOS from auto-zooming the viewport when a sub-16px form control is
// focused, without resizing any control (#581).
installIosZoomGuard();

const _ingressMatch = location.pathname.match(/^(\/api\/hassio_ingress\/[^/]+)/);
const _ingressBasename = _ingressMatch ? _ingressMatch[1] : "";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {/* The v7_startTransition / v7_relativeSplatPath opt-ins are gone: react-router
        dropped the `future` prop in 7.0 because both behaviours became the default. */}
    <BrowserRouter basename={_ingressBasename}>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
