import { useState } from 'react';
import {
  ACCENT_PRESETS, TYPE_PRESETS, DENSITY_PRESETS,
  loadAppearance, saveAppearance,
  type AccentId, type TypeId, type DensityId,
} from '@/lib/theme';
import { MarkerLabel } from '@/components/typography/MarkerLabel';

export function AppearanceSection() {
  const [prefs, setPrefs] = useState(() => loadAppearance());

  function handleAccent(id: AccentId) {
    setPrefs((p) => ({ ...p, accent: id }));
    saveAppearance({ accent: id });
  }
  function handleType(id: TypeId) {
    setPrefs((p) => ({ ...p, type: id }));
    saveAppearance({ type: id });
  }
  function handleDensity(id: DensityId) {
    setPrefs((p) => ({ ...p, density: id }));
    saveAppearance({ density: id });
  }

  return (
    <div className="space-y-8">
      <p className="text-sm text-muted-foreground">
        Personalize the look of JARVIS — choose an accent color, type pairing, and information density.
      </p>

      {/* Accent color */}
      <div>
        <MarkerLabel as="h3" className="mb-3">
          Accent color
        </MarkerLabel>
        <div className="flex gap-3 flex-wrap">
          {ACCENT_PRESETS.map((p) => (
            <button
              key={p.id}
              onClick={() => handleAccent(p.id)}
              title={p.label}
              aria-label={p.label}
              className={`flex flex-col items-center gap-1.5 p-2 rounded-lg border-2 transition-colors ${
                prefs.accent === p.id
                  ? 'border-[var(--ink-blue)]'
                  : 'border-transparent hover:border-hair'
              }`}
            >
              <span
                className="h-8 w-8 rounded-full block"
                style={{ backgroundColor: p.light }}
              />
              <span className="font-mono text-[10px] text-meta">{p.label}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Type pairing */}
      <div>
        <h3 className="font-mono text-[11px] uppercase tracking-widest text-meta mb-3">
          Type pairing
        </h3>
        <div className="flex gap-2 flex-wrap">
          {TYPE_PRESETS.map((p) => (
            <button
              key={p.id}
              onClick={() => handleType(p.id)}
              className={`px-3 py-2 rounded-md border text-left transition-colors ${
                prefs.type === p.id
                  ? 'border-[var(--ink-blue)] bg-[var(--ink-blue-soft)]'
                  : 'border-hair hover:border-[var(--ink-blue-border)]'
              }`}
            >
              <p className="text-[13px] font-medium text-strong">{p.label}</p>
              <p className="text-[10px] font-mono text-meta mt-0.5">{p.description}</p>
            </button>
          ))}
        </div>
      </div>

      {/* Density */}
      <div>
        <h3 className="font-mono text-[11px] uppercase tracking-widest text-meta mb-3">
          Density
        </h3>
        <div className="flex gap-2">
          {DENSITY_PRESETS.map((p) => (
            <button
              key={p.id}
              onClick={() => handleDensity(p.id)}
              className={`px-4 py-1.5 rounded-md border text-[12px] font-mono transition-colors ${
                prefs.density === p.id
                  ? 'border-[var(--ink-blue)] bg-[var(--ink-blue-soft)] text-[var(--ink-blue)]'
                  : 'border-hair text-meta hover:border-[var(--ink-blue-border)]'
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      {/* Reset */}
      <button
        onClick={() => {
          const defaults = { accent: 'ink-blue' as AccentId, type: 'serif-calm' as TypeId, density: 'default' as DensityId };
          setPrefs(defaults);
          saveAppearance(defaults);
        }}
        className="text-[11px] font-mono text-faint hover:text-meta transition-colors"
      >
        Reset to defaults
      </button>
    </div>
  );
}
