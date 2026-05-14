import { useState, useCallback, useRef, useEffect } from 'react';
import { MarkerCaption as SectionHeader } from '@/components/typography/MarkerCaption';
import { getJournalEntry, upsertJournalEntry } from '@/lib/api';
import type { JournalPrompts } from '@/types';

export function JournalSection() {
  const today = new Date().toISOString().split('T')[0] ?? new Date().toISOString().slice(0, 10);
  const [prompts, setPrompts] = useState<JournalPrompts>({});
  const [expanded, setExpanded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveAbortController = useRef<AbortController | null>(null);

  useEffect(() => {
    setLoadError(null);
    getJournalEntry(today).then((entry) => {
      if (entry) {
        setPrompts(entry.prompts);
        // Auto-expand if reflection fields have content
        if (entry.prompts.worked || entry.prompts.blocked) {
          setExpanded(true);
        }
      }
    }).catch((err) => {
      console.error('Journal load failed:', err);
      setLoadError(err instanceof Error ? err.message : 'Journal could not be loaded');
    });
  }, [today]);

  // Cleanup: clear any pending save timer and abort any in-flight fetch on unmount
  // to prevent setState-on-unmounted-component warnings.
  useEffect(() => {
    return () => {
      if (saveTimer.current) {
        clearTimeout(saveTimer.current);
        saveTimer.current = null;
      }
      saveAbortController.current?.abort();
      saveAbortController.current = null;
    };
  }, []);

  const scheduleSave = useCallback(
    (next: JournalPrompts) => {
    if (loadError) return;
    if (saveTimer.current) clearTimeout(saveTimer.current);
    // Abort any in-flight save before scheduling a new one
    saveAbortController.current?.abort();
    saveAbortController.current = null;
      setSaving(false);
      saveTimer.current = setTimeout(() => {
        const controller = new AbortController();
        saveAbortController.current = controller;
        setSaving(true);
        upsertJournalEntry(today, next, controller.signal)
          .then(() => setSaving(false))
          .catch((err) => {
            if (err instanceof DOMException && err.name === 'AbortError') return;
            console.error('Journal save failed:', err);
            setSaving(false);
          });
      }, 1500);
    },
    [today, loadError],
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
          <label htmlFor="journal-first-move" className="block font-mono text-[10px] text-meta mb-1">
            First move tomorrow?
          </label>
          <textarea
            id="journal-first-move"
            value={prompts.first_move ?? ''}
            onChange={(e) => update('first_move', e.target.value)}
            disabled={loadError != null}
            placeholder="What's the one thing you'll do first tomorrow?"
            rows={2}
            className="w-full resize-none rounded-md border border-hair bg-paper px-3 py-2 text-[13.5px] text-soft placeholder:text-zinc-300 dark:placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-[var(--ink-blue,#0b3a8a)]/40 transition-shadow"
          />
        </div>

        {/* Expand toggle */}
        {loadError && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-700 dark:text-amber-300">
            {loadError}
          </div>
        )}

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
              <label htmlFor="journal-worked" className="block font-mono text-[10px] text-meta mb-1">
                What worked?
              </label>
              <textarea
                id="journal-worked"
                value={prompts.worked ?? ''}
                onChange={(e) => update('worked', e.target.value)}
                disabled={loadError != null}
                placeholder="What went well today?"
                rows={2}
                className="w-full resize-none rounded-md border border-hair bg-paper px-3 py-2 text-[13.5px] text-soft placeholder:text-zinc-300 dark:placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-[var(--ink-blue,#0b3a8a)]/40 transition-shadow"
              />
            </div>

            <div>
              <label htmlFor="journal-blocked" className="block font-mono text-[10px] text-meta mb-1">
                What's blocked you?
              </label>
              <textarea
                id="journal-blocked"
                value={prompts.blocked ?? ''}
                onChange={(e) => update('blocked', e.target.value)}
                disabled={loadError != null}
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
