import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./styles.css";

const _ingressMatch = location.pathname.match(/^(\/api\/hassio_ingress\/[^/]+)/);
const _ingressBasename = _ingressMatch ? _ingressMatch[1] : "";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter
      basename={_ingressBasename}
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
