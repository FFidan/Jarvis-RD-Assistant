import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Legend,
} from 'recharts';
import type { LlmCostRow } from '@/types';

const WORKFLOW_COLORS = [
  'hsl(221, 83%, 53%)',
  'hsl(142, 71%, 45%)',
  'hsl(38, 92%, 50%)',
  'hsl(0, 84%, 60%)',
  'hsl(262, 83%, 58%)',
];

interface LlmCostChartProps {
  data: LlmCostRow[];
}

export function LlmCostChart({ data }: LlmCostChartProps) {
  // Pivot: group by day, with workflow as separate keys
  const workflows = [...new Set(data.map((r) => r.workflow))];
  const dayMap = new Map<string, Record<string, number>>();

  for (const row of data) {
    const entry = dayMap.get(row.day) ?? {};
    entry[row.workflow] = (entry[row.workflow] ?? 0) + row.total_cost;
    dayMap.set(row.day, entry);
  }

  const chartData = [...dayMap.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, costs]) => ({ day, ...costs }));

  return (
    <ResponsiveContainer width="100%" height={300}>
      <AreaChart data={chartData}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="day" tick={{ fontSize: 12 }} />
        <YAxis tickFormatter={(v: number) => `$${v.toFixed(2)}`} />
        <Tooltip formatter={(value) => [`$${Number(value).toFixed(4)}`, '']} />
        <Legend />
        {workflows.map((wf, idx) => (
          <Area
            key={wf}
            type="monotone"
            dataKey={wf}
            name={wf}
            stroke={WORKFLOW_COLORS[idx % WORKFLOW_COLORS.length]}
            fill={WORKFLOW_COLORS[idx % WORKFLOW_COLORS.length]}
            fillOpacity={0.3}
            stackId="1"
          />
        ))}
      </AreaChart>
    </ResponsiveContainer>
  );
}
