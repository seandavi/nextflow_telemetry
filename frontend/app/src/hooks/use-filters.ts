import { useSearchParams } from "react-router-dom";
import { useMemo, useCallback } from "react";
import type { MetricsFilters } from "@/types/metrics";

const WINDOW_OPTIONS = [
  { label: "7 days", value: "7" },
  { label: "30 days", value: "30" },
  { label: "90 days", value: "90" },
  { label: "180 days", value: "180" },
  { label: "All time", value: "all" },
] as const;

const LIMIT_OPTIONS = [
  { label: "Default", value: "default" },
  { label: "5", value: "5" },
  { label: "10", value: "10" },
  { label: "25", value: "25" },
  { label: "50", value: "50" },
  { label: "100", value: "100" },
] as const;

export { WINDOW_OPTIONS, LIMIT_OPTIONS };

export function useFilters() {
  const [searchParams, setSearchParams] = useSearchParams();

  const filters: MetricsFilters = useMemo(() => {
    const wd = searchParams.get("window_days");
    const lim = searchParams.get("limit");
    const ms = searchParams.get("min_samples");
    return {
      window_days: wd ? Number(wd) : undefined,
      limit: lim ? Number(lim) : undefined,
      min_samples: ms ? Number(ms) : undefined,
    };
  }, [searchParams]);

  const setFilter = useCallback(
    (key: keyof MetricsFilters, value: string) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        if (value === "all" || value === "default" || value === "" || value == null) {
          next.delete(key);
        } else {
          next.set(key, value);
        }
        return next;
      });
    },
    [setSearchParams],
  );

  return { filters, setFilter };
}
