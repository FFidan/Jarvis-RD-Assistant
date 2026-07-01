import { useState } from 'react';
import type { HardwareInfoApi } from './hardware-fit';

interface HardwareStripProps {
  hardware: HardwareInfoApi;
}

export function HardwareStrip({ hardware }: HardwareStripProps) {
  const [expanded, setExpanded] = useState(false);
  if (!hardware.vram_gb && hardware.tier === undefined) return null;

  const summary = [
    typeof hardware.vram_gb === 'number' ? `${hardware.vram_gb.toFixed(1)} GB VRAM` : null,
    typeof hardware.tier === 'number' ? `Tier ${hardware.tier}` : null,
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <div className="mb-3 space-y-1">
      <button
        type="button"
        aria-expanded={expanded}
        className="w-full cursor-pointer rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground select-none text-left"
        onClick={() => setExpanded((v) => !v)}
        data-testid="hardware-strip"
      >
        <span className="font-medium text-foreground">{summary}</span>
        {expanded && (
          <span className="ml-3 space-x-3">
            {hardware.vram_source && (
              <span>
                Source: <span className="text-foreground">{hardware.vram_source}</span>
              </span>
            )}
            {hardware.detected_at && (
              <span>
                Detected:{' '}
                <span className="text-foreground">
                  {new Date(hardware.detected_at).toLocaleString()}
                </span>
              </span>
            )}
          </span>
        )}
      </button>
      {hardware.vram_source_detail && (
        <p
          className="px-1 text-xs text-muted-foreground"
          data-testid="hardware-source-line"
        >
          {hardware.vram_source_detail}
        </p>
      )}
      {hardware.host_gpu_divergence === true && typeof hardware.vram_gb === 'number' && (
        <p
          className="rounded-md border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
          data-testid="gpu-overlay-divergence"
        >
          {hardware.vram_gb.toFixed(1)} GB detected on host — GPU overlay not active
        </p>
      )}
    </div>
  );
}
