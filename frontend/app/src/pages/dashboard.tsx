import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { QueryLoading, QueryError } from "@/components/query-state";
import { useSummary } from "@/api/metrics";
import { useFilters } from "@/hooks/use-filters";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from "recharts";

const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
];

export function DashboardPage() {
  const { filters } = useFilters();
  const query = useSummary(filters);

  if (query.isLoading) return <QueryLoading />;
  if (query.isError) return <QueryError query={query} label="summary" />;

  const data = query.data!;
  const { cards } = data;

  return (
    <div className="space-y-6">
      {/* Timestamp */}
      <p className="text-xs text-muted-foreground">
        Latest data: {new Date(cards.latest_process_completed_utc).toLocaleString()} |
        Generated: {new Date(data.generated_at_utc).toLocaleString()}
      </p>

      {/* Summary cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard title="Total Processes" value={cards.process_completed_rows.toLocaleString()} />
        <StatCard title="Distinct Runs" value={cards.distinct_runs.toLocaleString()} />
        <StatCard title="Failure Rate" value={`${cards.failure_pct}%`} subtitle={`${cards.failure_rows.toLocaleString()} failures`} />
        <StatCard title="Retry Rate" value={`${cards.retry_pct}%`} subtitle={`${cards.retried_rows.toLocaleString()} retried (${cards.retry_success_pct}% recovered)`} />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Top failures */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Top Failing Processes</CardTitle>
          </CardHeader>
          <CardContent>
            {data.top_failures.length === 0 ? (
              <p className="text-sm text-muted-foreground">No failures found.</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Process</TableHead>
                    <TableHead className="text-right">Failed</TableHead>
                    <TableHead className="text-right">Total</TableHead>
                    <TableHead className="text-right">Rate</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.top_failures.map((row) => (
                    <TableRow key={row.process}>
                      <TableCell className="font-mono text-sm">{row.process}</TableCell>
                      <TableCell className="text-right">{row.failed.toLocaleString()}</TableCell>
                      <TableCell className="text-right">{row.total_completed.toLocaleString()}</TableCell>
                      <TableCell className="text-right">
                        <Badge variant={row.failure_pct > 10 ? "destructive" : "secondary"}>
                          {row.failure_pct}%
                        </Badge>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        {/* Top retries */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Top Retried Processes</CardTitle>
          </CardHeader>
          <CardContent>
            {data.top_retries.length === 0 ? (
              <p className="text-sm text-muted-foreground">No retries found.</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Process</TableHead>
                    <TableHead className="text-right">Retried</TableHead>
                    <TableHead className="text-right">Rate</TableHead>
                    <TableHead className="text-right">Recovered</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.top_retries.map((row) => (
                    <TableRow key={row.process}>
                      <TableCell className="font-mono text-sm">{row.process}</TableCell>
                      <TableCell className="text-right">{row.retried.toLocaleString()}</TableCell>
                      <TableCell className="text-right">{row.retried_pct}%</TableCell>
                      <TableCell className="text-right">{row.retried_success.toLocaleString()}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Exit codes chart */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Top Failure Exit Codes</CardTitle>
        </CardHeader>
        <CardContent>
          {data.top_failure_exit_codes.length === 0 ? (
            <p className="text-sm text-muted-foreground">No failure exit codes found.</p>
          ) : (
            <ResponsiveContainer width="100%" height={250}>
              <BarChart data={data.top_failure_exit_codes} layout="vertical" margin={{ left: 60 }}>
                <XAxis type="number" />
                <YAxis type="category" dataKey="exit_code" width={50} tick={{ fontSize: 12 }} />
                <Tooltip formatter={(value) => Number(value).toLocaleString()} />
                <Bar dataKey="failures" radius={[0, 4, 4, 0]}>
                  {data.top_failure_exit_codes.map((_, i) => (
                    <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function StatCard({ title, value, subtitle }: { title: string; value: string; subtitle?: string }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold">{value}</div>
        {subtitle && <p className="text-xs text-muted-foreground mt-1">{subtitle}</p>}
      </CardContent>
    </Card>
  );
}
