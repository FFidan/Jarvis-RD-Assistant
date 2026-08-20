/**
 * ConfigSlider — labeled Slider row that commits on value-commit with an optional InfoTooltip.
 *
 * Immediate-apply is kept, but made visible and reversible: every commit
 * announces itself in a toast carrying an Undo action, so a stray click on the
 * track can no longer silently change a server setting.
 */
import { useEffect, useRef } from 'react';
import { toast } from 'sonner';
import { Slider } from '@/components/ui/slider';
import { Label } from '@/components/ui/label';
import { InfoTooltip } from '@/components/ui/info-tooltip';

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
}: ConfigSliderProps) {
  // Last value the server accepted; the Undo target.
  const committedRef = useRef(value);
  // Last value this control itself pushed up. Anything else arriving in
  // `value` came from the server (parents seed a fallback and reset on the
  // real config), and is the new saved value — without adopting it, dragging
  // back to the fallback looks like "no change" and never reaches the server.
  const lastLocalRef = useRef(value);

  useEffect(() => {
    if (value === lastLocalRef.current) return;
    lastLocalRef.current = value;
    committedRef.current = value;
  }, [value]);

  const handleLocalChange = (v: number) => {
    lastLocalRef.current = v;
    onLocalChange(v);
  };

  const handleCommit = (v: number) => {
    const previous = committedRef.current;
    if (v === previous) return; // click without movement — nothing to save
    committedRef.current = v;
    onCommit(v);
    toast.success(`${label} saved: ${v}${unit}`, {
      action: {
        label: `Undo (${previous}${unit})`,
        onClick: () => {
          committedRef.current = previous;
          handleLocalChange(previous);
          onCommit(previous);
        },
      },
    });
  };

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
        onValueChange={([v]) => handleLocalChange(v ?? value)}
        onValueCommit={([v]) => handleCommit(v ?? value)}
        disabled={disabled}
        className="w-full"
      />
      {description && <p className="text-xs text-muted-foreground">{description}</p>}
    </div>
  );
}
