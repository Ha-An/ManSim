import React from "react";
import ReactDOM from "react-dom/client";
import App from "./app/App";
import "./app/app.css";

type BootstrapErrorBoundaryState = {
  errorMessage: string | null;
};

class BootstrapErrorBoundary extends React.Component<React.PropsWithChildren, BootstrapErrorBoundaryState> {
  state: BootstrapErrorBoundaryState = {
    errorMessage: null,
  };

  static getDerivedStateFromError(error: unknown): BootstrapErrorBoundaryState {
    return {
      errorMessage: error instanceof Error ? error.stack || error.message : String(error),
    };
  }

  componentDidCatch(error: unknown): void {
    console.error("Replay Studio bootstrap error", error);
  }

  render(): React.ReactNode {
    if (this.state.errorMessage) {
      return (
        <div style={{ padding: 24, fontFamily: "Segoe UI, Arial, sans-serif", color: "#ffd6de", background: "#2a0f18", minHeight: "100vh" }}>
          <h1 style={{ marginTop: 0 }}>Replay Studio bootstrap error</h1>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 14, lineHeight: 1.5 }}>{this.state.errorMessage}</pre>
        </div>
      );
    }
    return this.props.children;
  }
}

window.addEventListener("error", (event) => {
  console.error("Replay Studio window error", event.error?.stack || event.message || "Unknown runtime error");
});

window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason instanceof Error ? event.reason.stack || event.reason.message : String(event.reason);
  console.error("Replay Studio unhandled rejection", reason);
});

const root = document.getElementById("root");
if (!root) {
  throw new Error("Replay Studio root element is missing.");
}

ReactDOM.createRoot(root).render(
  <BootstrapErrorBoundary>
    <App />
  </BootstrapErrorBoundary>,
);
