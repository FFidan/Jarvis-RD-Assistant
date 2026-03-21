import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from 'recharts';
import type { RetentionRow } from '@/types';

interface RetentionChartProps {
  data: RetentionRow[];
}

export function RetentionChart({ data }: RetentionChartProps) {
  const sorted = [...data].sort(
    (a, b) => new Date(a.review_date).getTime() - new Date(b.review_date).getTime(),
  );

  return (
    <ResponsiveContainer width="100%" height={300}>
      <LineChart data={sorted}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="review_date" tick={{ fontSize: 12 }} />
        <YAxis domain={[0, 100]} unit="%" />
        <Tooltip
          formatter={(value) => [`${value}%`, 'Retention']}
        />
        <Line
          type="monotone"
          dataKey="retention_pct"
          name="Retention %"
          stroke="hsl(142, 71%, 45%)"
          strokeWidth={2}
          dot={{ r: 3 }}
          activeDot={{ r: 5 }}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
