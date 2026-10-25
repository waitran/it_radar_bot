import { createRoot } from "react-dom/client";
import { App } from "./App";
import { initializeTelegram } from "./telegram";
import "./styles.css";

initializeTelegram();
createRoot(document.getElementById("root")!).render(<App />);
