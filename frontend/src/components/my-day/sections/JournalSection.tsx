import { useState, useCallback, useRef, useEffect } from 'react';
import { SectionHeader } from './SectionHeader';
import { getJournalEntry, upsertJournalEntry } from '@/lib/api';
import type { JournalPrompts } from '@/types';

export function JournalSection() {
  const today = new Date().toISOString().split('T')[0];
  const [prompts, setPrompts] = useState<JournalPrompts>({});
  const [expanded, setExpanded] = useState(false);
  const [saving, setSaving] = useState(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    getJournalEntry(today).then((entry) => {
      if (entry) {
        setPrompts(entry.prompts);
        // Auto-expand if reflection fields have content
        if (entry.prompts.worked || entry.prompts.blocked) {
          setExpanded(true);
        }
      }
    }).catch(console.error);
  }, [today]);

  // Cleanup: clear any pending save timer on unmount to prevent state updates on
  // an unmounted component and avoid network requests after navigation.
  useEffect(() => {
    return () => {
      if (saveTimer.current) {
        clearTimeout(saveTimer.current);
        saveTimer.current = null;
      }
    };
  }, []);

  const scheduleSave = useCallback(
    (next: JournalPrompts) => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      setSaving(false);
      saveTimer.current = setTimeout(() => {
        setSaving(true);
        upsertJournalEntry(today, next)
          .then(() => setSaving(false))
          .catch((err) => {
            console.error('Journal save failed:', err);
            setSaving(false);
          });
      }, 1500);
    },
    [today],
  );

  const update = (key: keyof JournalPrompts, value: string) => {
    const next = { ...prompts, [key]: value };
    setPrompts(next);
    scheduleSave(next);
  };

  return (
    <section id="journal">
      <SectionHeader
        marker="Journal"
        meta={saving ? 'saving…' : undefined}
      />

      <div className="space-y-3">
        {/* Primary prompt: First move tomorrow */}
        <div>
          <label className="block font-mono text-[10px] text-meta mb-1">
            First move tomorrow?
          </label>
          <textarea
            value={prompts.first_move ?? ''}
            onChange={(e) => update('first_move', e.target.value)}
            placeholder="What's the one thing you'll do first tomorrow?"
            rows={2}
            className="w-full resize-none rounded-md border border-hair bg-paper px-3 py-2 text-[13.5px] text-soft placeholder:text-zinc-300 dark:placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-[var(--ink-blue,#0b3a8a)]/40 transition-shadow"
          />
        </div>

        {/* Expand toggle */}
        {!expanded && (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="text-[11px] font-mono text-faint hover:text-soft transition-colors"
          >
            + add reflection
          </button>
        )}

        {/* Reflection fields — shown when expanded */}
        {expanded && (
          <>
            <div>
              <label className="block font-mono text-[10px] text-meta mb-1">
                What worked?
              </label>
              <textarea
                value={prompts.worked ?? ''}
                onChange={(e) => update('worked', e.target.value)}
                placeholder="What went well today?"
                rows={2}
                className="w-full resize-none rounded-md border border-hair bg-paper px-3 py-2 text-[13.5px] text-soft placeholder:text-zinc-300 dark:placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-[var(--ink-blue,#0b3a8a)]/40 transition-shadow"
              />
            </div>

            <div>
              <label className="block font-mono text-[10px] text-meta mb-1">
                What's blocked you?
              </label>
              <textarea
                value={prompts.blocked ?? ''}
                onChange={(e) => update('blocked', e.target.value)}
                placeholder="Any blockers or friction worth noting?"
                rows={2}
                className="w-full resize-none rounded-md border border-hair bg-paper px-3 py-2 text-[13.5px] text-soft placeholder:text-zinc-300 dark:placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-[var(--ink-blue,#0b3a8a)]/40 transition-shadow"
              />
            </div>

            <button
              type="button"
              onClick={() => setExpanded(false)}
              className="text-[11px] font-mono text-faint hover:text-soft transition-colors"
            >
              − collapse
            </button>
          </>
        )}
      </div>
    </section>
  );
}
