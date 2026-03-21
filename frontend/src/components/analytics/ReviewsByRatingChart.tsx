import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Cell,
} from 'recharts';
import type { ReviewRow } from '@/types';

const RATING_LABELS: Record<number, string> = {
  1: 'Again',
  2: 'Hard',
  3: 'Good',
  4: 'Easy',
};

const RATING_COLORS: Record<number, string> = {
  1: 'hsl(0, 84%, 60%)',
  2: 'hsl(38, 92%, 50%)',
  3: 'hsl(142, 71%, 45%)',
  4: 'hsl(221, 83%, 53%)',
};

interface ReviewsByRatingChartProps {
  data: ReviewRow[];
}

export function ReviewsByRatingChart({ data }: ReviewsByRatingChartProps) {
  const chartData = data.map((row) => ({
    ...row,
    label: RATING_LABELS[row.rating] ?? String(row.rating),
  }));

  return (
    <ResponsiveContainer width="100%" height={300}>
      <BarChart data={chartData}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="label" />
        <YAxis allowDecimals={false} />
        <Tooltip />
        <Bar dataKey="count" name="Reviews">
          {chartData.map((entry, idx) => (
            <Cell key={idx} fill={RATING_COLORS[entry.rating] ?? 'hsl(0, 0%, 70%)'} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
