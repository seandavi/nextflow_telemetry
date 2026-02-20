import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { QueryLoading, QueryError } from "@/components/query-state";
import { useResources } from "@/api/metrics";
import { useFilters } from "@/hooks/use-filters";

function fmt(v: number | null, suffix = ""): string {
  if (v == null) return "—";
  return `${v}${suffix}`;
}

export function ResourcesPage() {
  const { filters } = useFilters();
  const query = useResources(filters);

  if (query.isLoading) return <QueryLoading />;
  if (query.isError) return <QueryError query={query} label="resources" />;

  const data = query.data!;

  return (
    <div className="space-y-6">
      <p className="text-xs text-muted-foreground">
        Generated: {new Date(data.generated_at_utc).toLocaleString()}
      </p>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Resource Usage by Process & Attempt</CardTitle>
        </CardHeader>
        <CardContent>
          {data.rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">No resource data (min_samples threshold may be too high).</p>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Process</TableHead>
                    <TableHead className="text-right">Att.</TableHead>
                    <TableHead className="text-right">Rows</TableHead>
                    <TableHead className="text-right">CPUs</TableHead>
                    <TableHead className="text-right">Mem (GB)</TableHead>
                    <TableHead className="text-right">Avg %CPU</TableHead>
                    <TableHead className="text-right">P95 %CPU</TableHead>
                    <TableHead className="text-right">Avg %Mem</TableHead>
                    <TableHead className="text-right">P95 %Mem</TableHead>
                    <TableHead className="text-right">Avg RSS (GB)</TableHead>
                    <TableHead className="text-right">P95 RSS (GB)</TableHead>
                    <TableHead className="text-right">Read (GB)</TableHead>
                    <TableHead className="text-right">Write (GB)</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.rows.map((row) => (
                    <TableRow key={`${row.process}-${row.attempt}`}>
                      <TableCell className="font-mono text-sm">{row.process}</TableCell>
                      <TableCell className="text-right">{row.attempt}</TableCell>
                      <TableCell className="text-right">{row.rows.toLocaleString()}</TableCell>
                      <TableCell className="text-right">{fmt(row.avg_requested_cpus)}</TableCell>
                      <TableCell className="text-right">{fmt(row.avg_requested_memory_gb)}</TableCell>
                      <TableCell className="text-right">{fmt(row.avg_pct_cpu, "%")}</TableCell>
                      <TableCell className="text-right">{fmt(row.p95_pct_cpu, "%")}</TableCell>
                      <TableCell className="text-right">{fmt(row.avg_pct_mem, "%")}</TableCell>
                      <TableCell className="text-right">{fmt(row.p95_pct_mem, "%")}</TableCell>
                      <TableCell className="text-right">{fmt(row.avg_peak_rss_gb)}</TableCell>
                      <TableCell className="text-right">{fmt(row.p95_peak_rss_gb)}</TableCell>
                      <TableCell className="text-right">{fmt(row.avg_read_gb)}</TableCell>
                      <TableCell className="text-right">{fmt(row.avg_write_gb)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
