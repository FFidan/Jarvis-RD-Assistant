import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Legend,
} from 'recharts';
import type { ActivityRow } from '@/types';

interface ActivityChartProps {
  data: ActivityRow[];
}

export function ActivityChart({ data }: ActivityChartProps) {
  // Data arrives pre-sorted (ASC) from the backend
  return (
    <ResponsiveContainer width="100%" height={300}>
      <BarChart data={data}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="log_date" tick={{ fontSize: 12 }} />
        <YAxis allowDecimals={false} />
        <Tooltip />
        <Legend />
        <Bar dataKey="papers_read" name="Papers Read" fill="hsl(221, 83%, 53%)" />
        <Bar dataKey="cards_reviewed" name="Cards Reviewed" fill="hsl(142, 71%, 45%)" />
        <Bar dataKey="tasks_completed" name="Tasks Completed" fill="hsl(38, 92%, 50%)" />
      </BarChart>
    </ResponsiveContainer>
  );
}
