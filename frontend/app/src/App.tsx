import { BrowserRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Layout } from "@/components/layout";
import { DashboardPage } from "@/pages/dashboard";
import { RetriesPage } from "@/pages/retries";
import { ResourcesPage } from "@/pages/resources";
import { FailuresPage } from "@/pages/failures";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      retry: 1,
    },
  },
});

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<DashboardPage />} />
            <Route path="retries" element={<RetriesPage />} />
            <Route path="resources" element={<ResourcesPage />} />
            <Route path="failures" element={<FailuresPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

export default App;
