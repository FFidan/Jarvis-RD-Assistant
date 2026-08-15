import {
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  Tooltip,
  Legend,
} from 'recharts';
import type { StatusCountRow } from '@/types';
import { paperStateLabel } from '@/lib/labels/paperState';

const COLORS: Record<string, string> = {
  new: 'hsl(221, 83%, 53%)',
  reading: 'hsl(38, 92%, 50%)',
  read: 'hsl(142, 71%, 45%)',
  archived: 'hsl(220, 9%, 46%)',
  starred: 'hsl(262, 83%, 58%)',
};

const FALLBACK_COLOR = 'hsl(0, 0%, 70%)';

interface PapersByStatusChartProps {
  data: StatusCountRow[];
}

export function PapersByStatusChart({ data }: PapersByStatusChartProps) {
  const labeledData = data.map((entry) => ({
    ...entry,
    statusLabel: paperStateLabel(entry.status),
  }));
  return (
    <ResponsiveContainer width="100%" height={300}>
      <PieChart>
        <Pie
          data={labeledData}
          dataKey="count"
          nameKey="statusLabel"
          cx="50%"
          cy="50%"
          outerRadius={100}
          label={({ name, value }) => `${name} (${value})`}
        >
          {labeledData.map((entry, idx) => (
            <Cell key={idx} fill={COLORS[entry.status] ?? FALLBACK_COLOR} />
          ))}
        </Pie>
        <Tooltip />
        <Legend />
      </PieChart>
    </ResponsiveContainer>
  );
}
