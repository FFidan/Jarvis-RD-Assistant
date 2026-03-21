import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

const ENTITY_TYPES = ['All', 'method', 'dataset', 'metric', 'concept', 'institution', 'author'];

interface EntityTypeFilterProps {
  value: string;
  onChange: (value: string) => void;
}

export function EntityTypeFilter({ value, onChange }: EntityTypeFilterProps) {
  return (
    <div>
      <label className="mb-1 block text-sm font-medium">Entity Type</label>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger className="w-48">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {ENTITY_TYPES.map((t) => (
            <SelectItem key={t} value={t}>
              {t.charAt(0).toUpperCase() + t.slice(1)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
