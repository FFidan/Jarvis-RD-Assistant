/**
 * FocusedMoreSections — the "More" disclosure row at the foot of My Day.
 *
 * My Day keeps the daily loop above the fold (masthead, Now, intent, Pulse);
 * everything episodic or end-of-day is reached from here. Each chip expands
 * its real section inline, so nothing is duplicated or summarised.
 */

import { useState, type ComponentType } from 'react';
import { cn } from '@/lib/utils';

export interface DemotedSection {
  key: string;
  label: string;
  Component: ComponentType;
}

interface FocusedMoreSectionsProps {
  sections: DemotedSection[];
}

export function FocusedMoreSections({ sections }: FocusedMoreSectionsProps) {
  const [openKeys, setOpenKeys] = useState<Set<string>>(new Set());

  const toggle = (key: string) => {
    setOpenKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  return (
    <section id="more-sections" className="space-y-8">
      <div>
        <div className="flex items-baseline gap-3 mb-3">
          <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-meta">
            More
          </span>
          <span className="font-mono text-[10px] text-faint">
            open what you need — the rest stays out of the way
          </span>
        </div>
        <div className="flex flex-wrap gap-2">
          {sections.map(({ key, label }) => {
            const isOpen = openKeys.has(key);
            return (
              <button
                key={key}
                type="button"
                onClick={() => toggle(key)}
                aria-expanded={isOpen}
                aria-controls={`more-section-${key}`}
                className={cn(
                  // rounded-md rectangle: matches the app's interactive idiom
                  // (Button primitive) and My Day's mono-caption chips — the
                  // codebase reserves rounded-full for dots and switches.
                  'rounded-md border px-3 py-1.5 font-mono text-[11px] uppercase tracking-wide transition-colors',
                  isOpen
                    ? 'border-[var(--ink-blue,theme(colors.blue.600))] text-strong bg-accent'
                    : 'border-hair text-meta hover:text-strong hover:bg-accent',
                )}
              >
                {label}
                <span className="ml-1.5 text-faint">{isOpen ? '−' : '+'}</span>
              </button>
            );
          })}
        </div>
      </div>

      {sections
        .filter(({ key }) => openKeys.has(key))
        .map(({ key, Component }) => (
          <div key={key} id={`more-section-${key}`}>
            <Component />
          </div>
        ))}
    </section>
  );
}
