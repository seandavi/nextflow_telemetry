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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { QueryLoading, QueryError } from "@/components/query-state";
import { useFailures, useFailureSignatures } from "@/api/metrics";
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

export function FailuresPage() {
  const { filters } = useFilters();
  const failuresQuery = useFailures(filters);
  const signaturesQuery = useFailureSignatures(filters);

  const isLoading = failuresQuery.isLoading || signaturesQuery.isLoading;

  if (isLoading) return <QueryLoading />;

  return (
    <div className="space-y-6">
      {failuresQuery.data && (
        <p className="text-xs text-muted-foreground">
          Generated: {new Date(failuresQuery.data.generated_at_utc).toLocaleString()}
        </p>
      )}

      <Tabs defaultValue="by-process">
        <TabsList>
          <TabsTrigger value="by-process">By Process</TabsTrigger>
          <TabsTrigger value="signatures">Failure Signatures</TabsTrigger>
        </TabsList>

        <TabsContent value="by-process" className="space-y-6 mt-4">
          {failuresQuery.isError ? (
            <QueryError query={failuresQuery} label="failures" />
          ) : (
            <>
              {/* Chart */}
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Failures by Process</CardTitle>
                </CardHeader>
                <CardContent>
                  {failuresQuery.data!.rows.length === 0 ? (
                    <p className="text-sm text-muted-foreground">No failure data found.</p>
                  ) : (
                    <ResponsiveContainer width="100%" height={300}>
                      <BarChart
                        data={failuresQuery.data!.rows.slice(0, 10)}
                        layout="vertical"
                        margin={{ left: 120 }}
                      >
                        <XAxis type="number" />
                        <YAxis
                          type="category"
                          dataKey="process"
                          width={110}
                          tick={{ fontSize: 11 }}
                        />
                        <Tooltip formatter={(value) => Number(value).toLocaleString()} />
                        <Bar dataKey="failed" radius={[0, 4, 4, 0]}>
                          {failuresQuery.data!.rows.slice(0, 10).map((_, i) => (
                            <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
                          ))}
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  )}
                </CardContent>
              </Card>

              {/* Table */}
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Failure Details</CardTitle>
                </CardHeader>
                <CardContent>
                  {failuresQuery.data!.rows.length === 0 ? (
                    <p className="text-sm text-muted-foreground">No failure data found.</p>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Process</TableHead>
                          <TableHead className="text-right">Total</TableHead>
                          <TableHead className="text-right">Success</TableHead>
                          <TableHead className="text-right">Failed</TableHead>
                          <TableHead className="text-right">Rate</TableHead>
                          <TableHead className="text-right">Top Exit Code</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {failuresQuery.data!.rows.map((row) => (
                          <TableRow key={row.process}>
                            <TableCell className="font-mono text-sm">{row.process}</TableCell>
                            <TableCell className="text-right">{row.total_completed.toLocaleString()}</TableCell>
                            <TableCell className="text-right">{row.success.toLocaleString()}</TableCell>
                            <TableCell className="text-right">{row.failed.toLocaleString()}</TableCell>
                            <TableCell className="text-right">
                              <Badge variant={row.failure_pct > 10 ? "destructive" : "secondary"}>
                                {row.failure_pct}%
                              </Badge>
                            </TableCell>
                            <TableCell className="text-right font-mono">
                              {row.modal_failure_exit_code ?? "—"}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </CardContent>
              </Card>
            </>
          )}
        </TabsContent>

        <TabsContent value="signatures" className="mt-4">
          {signaturesQuery.isError ? (
            <QueryError query={signaturesQuery} label="failure signatures" />
          ) : (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Failure Signatures (Process + Exit Code)</CardTitle>
              </CardHeader>
              <CardContent>
                {signaturesQuery.data!.rows.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No failure signatures found.</p>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Process</TableHead>
                        <TableHead className="text-right">Exit Code</TableHead>
                        <TableHead className="text-right">Failures</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {signaturesQuery.data!.rows.map((row, i) => (
                        <TableRow key={i}>
                          <TableCell className="font-mono text-sm">{row.process}</TableCell>
                          <TableCell className="text-right font-mono">{row.exit_code}</TableCell>
                          <TableCell className="text-right">{row.failures.toLocaleString()}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
