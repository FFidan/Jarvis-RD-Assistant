import { Badge } from '@/components/ui/badge';

const TYPE_COLORS: Record<string, string> = {
  method: '#1f77b4',
  dataset: '#2ca02c',
  metric: '#ff7f0e',
  concept: '#9467bd',
  institution: '#d62728',
  author: '#8c564b',
};

interface EntityBreakdownProps {
  counts: Record<string, number>;
}

export function EntityBreakdown({ counts }: EntityBreakdownProps) {
  const sorted = Object.entries(counts).sort(([, a], [, b]) => b - a);

  if (sorted.length === 0) return null;

  return (
    <div className="space-y-2">
      <h4 className="text-sm font-medium">Entities by Type</h4>
      <div className="flex flex-wrap gap-2">
        {sorted.map(([type, count]) => (
          <Badge
            key={type}
            variant="outline"
            style={{
              borderColor: TYPE_COLORS[type] || '#999',
              color: TYPE_COLORS[type] || '#999',
            }}
          >
            {type}: {count}
          </Badge>
        ))}
      </div>
    </div>
  );
}
