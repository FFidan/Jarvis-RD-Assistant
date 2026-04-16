import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

interface TimeSelectProps {
  value: string;        // "HH:MM" format
  onChange: (v: string) => void;
  disabled?: boolean;
}

export function TimeSelect({ value, onChange, disabled }: TimeSelectProps) {
  const [h = '04', m = '00'] = value.split(':');
  const hours = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0'));
  const minutes = ['00', '15', '30', '45'];
  return (
    <div className="flex items-center gap-1">
      <Select value={h} onValueChange={v => onChange(`${v}:${m}`)} disabled={disabled}>
        <SelectTrigger className="w-[70px]"><SelectValue /></SelectTrigger>
        <SelectContent>
          {hours.map(hr => <SelectItem key={hr} value={hr}>{hr}</SelectItem>)}
        </SelectContent>
      </Select>
      <span className="text-muted-foreground">:</span>
      <Select value={m} onValueChange={v => onChange(`${h}:${v}`)} disabled={disabled}>
        <SelectTrigger className="w-[70px]"><SelectValue /></SelectTrigger>
        <SelectContent>
          {minutes.map(mn => <SelectItem key={mn} value={mn}>{mn}</SelectItem>)}
        </SelectContent>
      </Select>
    </div>
  );
}
