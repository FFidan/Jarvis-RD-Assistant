import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

export type LayoutType = 'cose' | 'breadthfirst' | 'circle' | 'concentric';

interface GraphControlsProps {
  layout: LayoutType;
  onLayoutChange: (layout: LayoutType) => void;
}

const LAYOUTS: { value: LayoutType; label: string }[] = [
  { value: 'cose', label: 'Force-directed' },
  { value: 'breadthfirst', label: 'Breadth-first' },
  { value: 'circle', label: 'Circle' },
  { value: 'concentric', label: 'Concentric' },
];

export function GraphControls({ layout, onLayoutChange }: GraphControlsProps) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-sm font-medium text-muted-foreground">Layout:</span>
      <Select value={layout} onValueChange={(v) => onLayoutChange(v as LayoutType)}>
        <SelectTrigger className="w-48">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {LAYOUTS.map((l) => (
            <SelectItem key={l.value} value={l.value}>
              {l.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
