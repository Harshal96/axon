import React from "react";
import { createRoot } from "react-dom/client";
import "../base/tokens.css";
import "../base/primitives.css";
import "./app.css";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode><App /></React.StrictMode>,
);
