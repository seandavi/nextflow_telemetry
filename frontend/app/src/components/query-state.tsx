import type { UseQueryResult } from "@tanstack/react-query";

interface Props {
  query: UseQueryResult<unknown, Error>;
  label?: string;
}

export function QueryLoading({ label = "Loading..." }: { label?: string }) {
  return (
    <div className="flex items-center justify-center py-12 text-muted-foreground">
      <div className="animate-pulse">{label}</div>
    </div>
  );
}

export function QueryError({ query, label = "data" }: Props) {
  if (!query.isError) return null;
  return (
    <div className="rounded-md border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
      Failed to load {label}: {query.error.message}
    </div>
  );
}
