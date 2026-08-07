import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./styles.css";

const _ingressMatch = location.pathname.match(/^(\/api\/hassio_ingress\/[^/]+)/);
const _ingressBasename = _ingressMatch ? _ingressMatch[1] : "";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {/* The v7_startTransition / v7_relativeSplatPath opt-ins are gone: react-router 7
        removed the `future` prop because both behaviours are now the default. */}
    <BrowserRouter basename={_ingressBasename}>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
