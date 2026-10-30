import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { DashboardProvider } from "./state/store";
import "./styles/tactical.css";

const rootElement = document.getElementById("root");
if (rootElement) {
  ReactDOM.createRoot(rootElement).render(
    <React.StrictMode>
      <DashboardProvider>
        <App />
      </DashboardProvider>
    </React.StrictMode>,
  );
}
