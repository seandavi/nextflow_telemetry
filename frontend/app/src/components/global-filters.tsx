import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useFilters, WINDOW_OPTIONS, LIMIT_OPTIONS } from "@/hooks/use-filters";

export function GlobalFilters() {
  const { filters, setFilter } = useFilters();

  return (
    <div className="flex items-center gap-3">
      <div className="flex items-center gap-1.5">
        <label className="text-sm text-muted-foreground whitespace-nowrap">Window</label>
        <Select
          value={filters.window_days != null ? String(filters.window_days) : "all"}
          onValueChange={(v) => setFilter("window_days", v)}
        >
          <SelectTrigger className="w-[120px] h-8 text-sm">
            <SelectValue placeholder="All time" />
          </SelectTrigger>
          <SelectContent>
            {WINDOW_OPTIONS.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="flex items-center gap-1.5">
        <label className="text-sm text-muted-foreground whitespace-nowrap">Limit</label>
        <Select
          value={filters.limit != null ? String(filters.limit) : "default"}
          onValueChange={(v) => setFilter("limit", v)}
        >
          <SelectTrigger className="w-[80px] h-8 text-sm">
            <SelectValue placeholder="Default" />
          </SelectTrigger>
          <SelectContent>
            {LIMIT_OPTIONS.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}
