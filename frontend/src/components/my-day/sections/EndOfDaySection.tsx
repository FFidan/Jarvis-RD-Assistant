import { useState, useCallback, useRef, useEffect } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { toast } from 'sonner';
import { MarkerCaption as SectionHeader } from '@/components/typography/MarkerCaption';
import {
  getJournalEntry,
  upsertJournalEntry,
  fetchMyDay,
  seedThreadFromEod,
} from '@/lib/api';
import type { JournalPrompts, MyDayResponse } from '@/types';

/**
 * End of day — "shutdown ritual that closes the loop".
 *
 * Three structured prompts mapped to EOD's cognitive functions:
 *   • worked      — progress capture
 *   • blocked     — open-loop / Zeigarnik discharge (can spawn a thread)
 *   • first_move  — next-day context restoration (seeds tomorrow's intent)
 * Plus one optional free-note escape hatch. Pre-filled from the day's
 * mechanical signals (tasks done, focus hours) so the user reacts/edits
 * rather than facing a blank page. Persisted via the existing journal
 * POST-upsert (additive `note`); reused, no new backend.
 */
export function EndOfDaySection() {
  const today = new Date().toISOString().split('T')[0] ?? new Date().toISOString().slice(0, 10);
  const [prompts, setPrompts] = useState<JournalPrompts>({});
  const [saving, setSaving] = useState(false);
  const [showNote, setShowNote] = useState(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveAbort = useRef<AbortController | null>(null);
  const hasInitialized = useRef(false);

  const { data: journalEntry, error: loadQueryError } = useQuery({
    queryKey: QUERY_KEYS.journal.entry(today),
    queryFn: ({ signal }) => getJournalEntry(today, { signal }),
  });

  // Day signals for prefill placeholders (mechanical, not generative).
  const { data: myDay } = useQuery<MyDayResponse>({
    queryKey: QUERY_KEYS.myDay.today(),
    queryFn: fetchMyDay,
  });

  const seedThreadMutation = useMutation({
    mutationFn: (title: string) => seedThreadFromEod({ title }),
    onSuccess: (res) => {
      toast.success(
        res.created ? 'Blocker is now an open thread' : 'Linked to an existing thread',
      );
    },
    onError: (err: Error) => toast.error(`Couldn't create thread: ${err.message}`),
  });

  useEffect(() => {
    if (journalEntry && !hasInitialized.current) {
      setPrompts(journalEntry.prompts);
      hasInitialized.current = true;
      if (journalEntry.prompts.note) setShowNote(true);
    }
  }, [journalEntry]);

  const loadError = loadQueryError
    ? loadQueryError instanceof Error
      ? loadQueryError.message
      : 'Journal could not be loaded'
    : null;

  useEffect(() => {
    return () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveAbort.current?.abort();
    };
  }, []);

  const scheduleSave = useCallback(
    (next: JournalPrompts) => {
      if (loadError) return;
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveAbort.current?.abort();
      saveAbort.current = null;
      setSaving(false);
      saveTimer.current = setTimeout(() => {
        const controller = new AbortController();
        saveAbort.current = controller;
        setSaving(true);
        upsertJournalEntry(today, next, controller.signal)
          .then(() => setSaving(false))
          .catch((err) => {
            if (err instanceof DOMException && err.name === 'AbortError') return;
            console.error('Journal save failed:', err);
            setSaving(false);
          });
      }, 1200);
    },
    [today, loadError],
  );

  const update = (key: keyof JournalPrompts, value: string) => {
    const next = { ...prompts, [key]: value };
    setPrompts(next);
    scheduleSave(next);
  };

  // Mechanical prefill hints derived from the day's signals.
  const doneCount = myDay?.tasks.filter((t) => t.status === 'done').length ?? 0;
  const focusHours = myDay?.today_focus_hours ?? 0;
  const workedPlaceholder =
    doneCount > 0
      ? `e.g. closed ${doneCount} task${doneCount === 1 ? '' : 's'}, ${focusHours.toFixed(1)}h focused…`
      : 'One concrete thing that moved forward today…';

  const fields: {
    key: keyof JournalPrompts;
    label: string;
    placeholder: string;
  }[] = [
    { key: 'worked', label: 'One thing that worked', placeholder: workedPlaceholder },
    {
      key: 'blocked',
      label: "What's still blocking me",
      placeholder: 'The thing you keep bouncing off — name it so it stops looping.',
    },
    {
      key: 'first_move',
      label: 'First move tomorrow',
      placeholder: 'The single first action — sets tomorrow’s intent.',
    },
  ];

  const blockedText = (prompts.blocked ?? '').trim();

  return (
    <section id="eod" className="pb-4">
      <SectionHeader
        marker="End of day"
        meta={
          loadError
            ? undefined
            : saving
              ? 'saving…'
              : '3 prompts · closes the loop · saves to journal'
        }
      />

      {loadError && (
        <div className="mb-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-700 dark:text-amber-300">
          {loadError}
        </div>
      )}

      <div className="space-y-5">
        {fields.map((f) => (
          <div key={f.key}>
            <div className="mb-1.5 flex items-center justify-between">
              <label
                htmlFor={`eod-${f.key}`}
                className="font-mono text-[10.5px] uppercase tracking-[0.15em] text-meta"
              >
                {f.label}
              </label>
              {f.key === 'blocked' && blockedText.length > 0 && (
                <button
                  type="button"
                  onClick={() => seedThreadMutation.mutate(blockedText)}
                  disabled={seedThreadMutation.isPending}
                  className="font-mono text-[10px] text-[var(--ink-blue,#0b3a8a)] hover:underline disabled:opacity-50"
                >
                  make this a thread →
                </button>
              )}
            </div>
            <input
              id={`eod-${f.key}`}
              value={prompts[f.key] ?? ''}
              onChange={(e) => update(f.key, e.target.value)}
              disabled={loadError != null}
              placeholder={f.placeholder}
              className="w-full border-0 border-b border-dashed border-hair bg-transparent px-0 py-1.5 font-serif italic text-[14.5px] text-soft placeholder:text-faint focus:border-[var(--ink-blue,#0b3a8a)] focus:outline-none transition-colors"
            />
            {f.key === 'first_move' && (prompts.first_move ?? '').trim().length > 0 && (
              <p className="mt-1 font-mono text-[10px] text-faint">
                ↳ saved to tonight’s shutdown journal
              </p>
            )}
          </div>
        ))}

        {/* Optional free-note escape hatch */}
        {showNote ? (
          <div>
            <label
              htmlFor="eod-note"
              className="mb-1.5 block font-mono text-[10.5px] uppercase tracking-[0.15em] text-meta"
            >
              Anything else
            </label>
            <textarea
              id="eod-note"
              value={prompts.note ?? ''}
              onChange={(e) => update('note', e.target.value)}
              disabled={loadError != null}
              placeholder="Free space — not a prompt, just a release valve."
              rows={2}
              className="w-full resize-none rounded-md border border-hair bg-paper px-3 py-2 text-[13.5px] text-soft placeholder:text-faint focus:outline-none focus:ring-1 focus:ring-[var(--ink-blue,#0b3a8a)]/40 transition-shadow"
            />
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setShowNote(true)}
            className="font-mono text-[11px] text-faint hover:text-soft transition-colors"
          >
            + anything else
          </button>
        )}
      </div>
    </section>
  );
}
