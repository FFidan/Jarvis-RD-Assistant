import { Card, CardContent } from '@/components/ui/card';

interface Stat {
  label: string;
  value: number | string;
}

interface GraphStatsProps {
  stats: Stat[];
}

export function GraphStats({ stats }: GraphStatsProps) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {stats.map((s) => (
        <Card key={s.label}>
          <CardContent className="p-4 text-center">
            <p className="text-2xl font-bold">{s.value}</p>
            <p className="text-xs text-muted-foreground">{s.label}</p>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
