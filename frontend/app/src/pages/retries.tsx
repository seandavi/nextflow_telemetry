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
import { useRetries } from "@/api/metrics";
import { useFilters } from "@/hooks/use-filters";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";

export function RetriesPage() {
  const { filters } = useFilters();
  const query = useRetries(filters);

  if (query.isLoading) return <QueryLoading />;
  if (query.isError) return <QueryError query={query} label="retries" />;

  const data = query.data!;
  const { summary } = data;

  return (
    <div className="space-y-6">
      <p className="text-xs text-muted-foreground">
        Generated: {new Date(data.generated_at_utc).toLocaleString()}
      </p>

      {/* Summary cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <SummaryCard title="Total Processes" value={summary.process_completed_rows.toLocaleString()} />
        <SummaryCard title="Retried" value={summary.retried_rows.toLocaleString()} subtitle={`${summary.retried_pct}% of all`} />
        <SummaryCard title="Retry Success" value={summary.retry_success_rows.toLocaleString()} subtitle={`${summary.retry_success_pct}% of retried`} />
        <SummaryCard title="Retry Failures" value={summary.retry_failure_rows.toLocaleString()} />
      </div>

      {/* By attempt chart */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Outcomes by Attempt Number</CardTitle>
        </CardHeader>
        <CardContent>
          {data.by_attempt.length === 0 ? (
            <p className="text-sm text-muted-foreground">No attempt data available.</p>
          ) : (
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={data.by_attempt}>
                <XAxis dataKey="attempt" label={{ value: "Attempt", position: "insideBottom", offset: -5 }} />
                <YAxis />
                <Tooltip formatter={(value) => Number(value).toLocaleString()} />
                <Legend />
                <Bar dataKey="success" stackId="a" fill="var(--chart-2)" name="Success" />
                <Bar dataKey="failed" stackId="a" fill="var(--chart-1)" name="Failed" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      </Card>

      {/* By process table */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Retries by Process</CardTitle>
        </CardHeader>
        <CardContent>
          {data.by_process.length === 0 ? (
            <p className="text-sm text-muted-foreground">No process data (min_samples threshold may be too high).</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Process</TableHead>
                  <TableHead className="text-right">Total</TableHead>
                  <TableHead className="text-right">Retried</TableHead>
                  <TableHead className="text-right">Rate</TableHead>
                  <TableHead className="text-right">Recovered</TableHead>
                  <TableHead className="text-right">Failed</TableHead>
                  <TableHead className="text-right">Max Attempt</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.by_process.map((row) => (
                  <TableRow key={row.process}>
                    <TableCell className="font-mono text-sm">{row.process}</TableCell>
                    <TableCell className="text-right">{row.total_completed.toLocaleString()}</TableCell>
                    <TableCell className="text-right">{row.retried.toLocaleString()}</TableCell>
                    <TableCell className="text-right">
                      <Badge variant={row.retried_pct > 10 ? "destructive" : "secondary"}>
                        {row.retried_pct}%
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right">{row.retried_success.toLocaleString()}</TableCell>
                    <TableCell className="text-right">{row.retried_failed.toLocaleString()}</TableCell>
                    <TableCell className="text-right">{row.max_attempt}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function SummaryCard({ title, value, subtitle }: { title: string; value: string; subtitle?: string }) {
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
