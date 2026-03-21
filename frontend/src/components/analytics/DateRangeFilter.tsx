import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

const PRESETS = [
  { label: 'Last 7 days', days: 7 },
  { label: 'Last 30 days', days: 30 },
  { label: 'Last 90 days', days: 90 },
] as const;

interface DateRangeFilterProps {
  value: number;
  onChange: (days: number) => void;
}

export function DateRangeFilter({ value, onChange }: DateRangeFilterProps) {
  const [customDays, setCustomDays] = useState('');

  const handleCustomSubmit = () => {
    const parsed = parseInt(customDays, 10);
    if (parsed > 0 && parsed <= 365) {
      onChange(parsed);
    }
  };

  return (
    <div className="flex flex-wrap items-end gap-3">
      {PRESETS.map((preset) => (
        <Button
          key={preset.days}
          variant={value === preset.days ? 'default' : 'outline'}
          size="sm"
          onClick={() => onChange(preset.days)}
        >
          {preset.label}
        </Button>
      ))}
      <div className="flex items-end gap-2">
        <div>
          <Label htmlFor="custom-days" className="text-xs text-muted-foreground">
            Custom
          </Label>
          <Input
            id="custom-days"
            type="number"
            min={1}
            max={365}
            placeholder="Days"
            className="w-20"
            value={customDays}
            onChange={(e) => setCustomDays(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleCustomSubmit()}
          />
        </div>
        <Button variant="outline" size="sm" onClick={handleCustomSubmit}>
          Apply
        </Button>
      </div>
    </div>
  );
}
