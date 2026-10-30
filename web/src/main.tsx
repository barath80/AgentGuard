import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { ApiError } from "./lib/api";
import { AuthProvider, useAuth } from "./lib/auth";
import { Activity } from "./pages/Activity";
import { Agents } from "./pages/Agents";
import { Approvals } from "./pages/Approvals";
import { Insights } from "./pages/Insights";
import { Login } from "./pages/Login";
import { Overview } from "./pages/Overview";
import { Policies } from "./pages/Policies";
import { Tools } from "./pages/Tools";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      refetchOnWindowFocus: true,
      // Auth and permission errors won't fix themselves; don't hammer the gateway retrying them.
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
    },
  },
});

function App() {
  const { me, loading } = useAuth();
  if (loading) {
    return (
      <div className="grid min-h-screen place-items-center text-ink-3" aria-busy="true">
        Loading…
      </div>
    );
  }
  if (!me) return <Login />;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Overview />} />
        <Route path="activity" element={<Activity />} />
        <Route path="activity/:id" element={<Activity />} />
        <Route path="approvals" element={<Approvals />} />
        <Route path="agents" element={<Agents />} />
        <Route path="tools" element={<Tools />} />
        <Route path="policies" element={<Policies />} />
        <Route path="insights" element={<Insights />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
