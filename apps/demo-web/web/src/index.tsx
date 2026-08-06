import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("root element missing");
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      // Never poll — live updates via WebSocket invalidation only.
      refetchOnWindowFocus: false,
    },
  },
});

createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
