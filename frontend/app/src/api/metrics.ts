import { useQuery } from "@tanstack/react-query";
import type {
  MetricsFilters,
  SummaryResponse,
  RetriesResponse,
  ResourcesResponse,
  FailuresResponse,
  FailureSignaturesResponse,
} from "@/types/metrics";

function buildParams(filters: MetricsFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.window_days != null) params.set("window_days", String(filters.window_days));
  if (filters.limit != null) params.set("limit", String(filters.limit));
  if (filters.min_samples != null) params.set("min_samples", String(filters.min_samples));
  return params;
}

async function fetchJson<T>(path: string, filters: MetricsFilters = {}): Promise<T> {
  const params = buildParams(filters);
  const qs = params.toString();
  const url = qs ? `${path}?${qs}` : path;
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export function useSummary(filters: MetricsFilters = {}) {
  return useQuery({
    queryKey: ["summary", filters],
    queryFn: () => fetchJson<SummaryResponse>("/metrics/processes/summary", filters),
  });
}

export function useRetries(filters: MetricsFilters = {}) {
  return useQuery({
    queryKey: ["retries", filters],
    queryFn: () => fetchJson<RetriesResponse>("/metrics/processes/retries", filters),
  });
}

export function useResources(filters: MetricsFilters = {}) {
  return useQuery({
    queryKey: ["resources", filters],
    queryFn: () => fetchJson<ResourcesResponse>("/metrics/processes/resources-by-attempt", filters),
  });
}

export function useFailures(filters: MetricsFilters = {}) {
  return useQuery({
    queryKey: ["failures", filters],
    queryFn: () => fetchJson<FailuresResponse>("/metrics/processes/failures", filters),
  });
}

export function useFailureSignatures(filters: MetricsFilters = {}) {
  return useQuery({
    queryKey: ["failure-signatures", filters],
    queryFn: () => fetchJson<FailureSignaturesResponse>("/metrics/processes/failure-signatures", filters),
  });
}
