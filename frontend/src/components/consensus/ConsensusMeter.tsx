import { useState } from 'react';
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  CartesianGrid,
} from 'recharts';
import type { ConsensusClaim } from '@/types';

interface ConsensusMeterProps {
  data: ConsensusClaim[];
}

function truncate(label: string, max = 32): string {
  return label.length > max ? `${label.slice(0, max - 1)}…` : label;
}

/**
 * Horizontal stacked bars: one row per shared claim, split into the count of
 * supporting vs opposing related-paper assessments.
 */
export function ConsensusMeter({ data }: ConsensusMeterProps) {
  const [containerWidth, setContainerWidth] = useState(0);
  const rows = data.map((claim) => ({
    claim_topic: claim.claim_topic,
    supports: claim.supports,
    opposes: claim.opposes,
  }));
  const height = Math.max(160, rows.length * 56);
  const { axisWidth, labelLength } = consensusAxisLayout(containerWidth);
  return (
    <ResponsiveContainer width="100%" height={height} onResize={(width) => setContainerWidth(width)}>
      <BarChart data={rows} layout="vertical" margin={{ left: 16, right: 16 }}>
        <CartesianGrid strokeDasharray="3 3" horizontal={false} />
        <XAxis type="number" allowDecimals={false} />
        <YAxis
          type="category"
          dataKey="claim_topic"
          width={axisWidth}
          tick={{ fontSize: 12 }}
          tickFormatter={(label: string) => truncate(label, labelLength)}
        />
        <Tooltip />
        <Legend />
        <Bar dataKey="supports" name="Supports" stackId="stance" fill="hsl(142, 71%, 45%)" />
        <Bar dataKey="opposes" name="Opposes" stackId="stance" fill="hsl(0, 72%, 51%)" />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function consensusAxisLayout(containerWidth: number): {
  axisWidth: number;
  labelLength: number;
} {
  if (containerWidth <= 0 || containerWidth >= 500) {
    return { axisWidth: 200, labelLength: 32 };
  }
  return {
    axisWidth: Math.max(80, Math.floor(containerWidth * 0.4)),
    labelLength: 14,
  };
}
