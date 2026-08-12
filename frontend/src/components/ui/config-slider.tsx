/**
 * ConfigSlider — labeled Slider row that commits on value-commit with an optional InfoTooltip.
 */
import { Slider } from '@/components/ui/slider';
import { Label } from '@/components/ui/label';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { toast } from 'sonner';
import { errorMessage } from '@/lib/errors';

interface ConfigSliderProps {
  id?: string;
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit?: string;
  infoTooltip?: string;
  description?: string;
  disabled?: boolean;
  onLocalChange: (v: number) => void;
  onCommit: (v: number) => void;
  commitErrorLabel?: string;
}

export function ConfigSlider({
  id,
  label,
  value,
  min,
  max,
  step,
  unit = '',
  infoTooltip,
  description,
  disabled,
  onLocalChange,
  onCommit,
  commitErrorLabel,
}: ConfigSliderProps) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id} className="flex items-center justify-between">
        <span className="flex items-center gap-1">
          {label}
          {infoTooltip && <InfoTooltip content={infoTooltip} />}
        </span>
        <span className="text-muted-foreground text-sm font-normal">
          {value}
          {unit}
        </span>
      </Label>
      <Slider
        id={id}
        // The label above is tied to the slider's outer element and also holds the
        // current value, so it names nothing the control reports and would change
        // on every step. The control carries its own steady name.
        aria-label={label}
        min={min}
        max={max}
        step={step}
        value={[value]}
        onValueChange={([v]) => onLocalChange(v ?? value)}
        onValueCommit={([v]) =>
          onCommit !== undefined &&
          (commitErrorLabel
            ? void (async () => {
                try {
                  onCommit(v ?? value);
                } catch (err) {
                  toast.error(`Failed to update ${commitErrorLabel}`, {
                    description: errorMessage(err),
                  });
                }
              })()
            : onCommit(v ?? value))
        }
        disabled={disabled}
        className="w-full"
      />
      {description && <p className="text-xs text-muted-foreground">{description}</p>}
    </div>
  );
}
