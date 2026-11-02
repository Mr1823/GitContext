import React from "react";
import { createRoot } from "react-dom/client";
import { Toaster } from "sileo";
import App from "./App.jsx";
import "sileo/styles.css";
import "./sileo-overrides.css";

createRoot(document.getElementById("app-root")).render(
  <React.StrictMode>
    <App />
    <Toaster position="top-right" theme="dark" offset={{ top: 20, right: 20 }} />
  </React.StrictMode>,
);
