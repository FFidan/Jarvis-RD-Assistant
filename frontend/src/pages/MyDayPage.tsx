import { useEffect } from 'react';
import { useLocation } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { DateMasthead } from '@/components/my-day/sections/DateMasthead';
import { YesterdaySection } from '@/components/my-day/sections/YesterdaySection';
import { HeroNow } from '@/components/my-day/sections/HeroNow';
import { IntentSection } from '@/components/my-day/sections/IntentSection';
import { ProjectsSection } from '@/components/my-day/sections/ProjectsSection';
import { ThreadsSection } from '@/components/my-day/sections/ThreadsSection';
import { TodaysPulseSection } from '@/components/my-day/sections/TodaysPulseSection';
import { TriageSection } from '@/components/my-day/sections/TriageSection';
import { LearningFocusSection } from '@/components/my-day/sections/LearningFocusSection';
import { WeeklyDigestSection } from '@/components/my-day/sections/WeeklyDigestSection';
import { EndOfDaySection } from '@/components/my-day/sections/EndOfDaySection';
import { MyDayFooter } from '@/components/my-day/sections/MyDayFooter';
import { getMyDayBundle } from '@/lib/api';
import type { MyDayBundle } from '@/types';

/** Today's ISO date string (YYYY-MM-DD) in local time — matches EndOfDaySection. */
function todayIso(): string {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

export function MyDayPage() {
  const entryNum = Math.floor((Date.now() - new Date('2026-01-01').getTime()) / 86400000);
  const { hash } = useLocation();
  const queryClient = useQueryClient();

  // Browser tz offset (minutes EAST of UTC) — matches YesterdaySection convention.
  const tzOffsetMinutes = -new Date().getTimezoneOffset();

  // ── My-Day bundle: single round-trip that primes per-section caches ────────
  //
  // Sections that can be satisfied from the bundle:
  //   threads    → ['my-day', 'threads']  (ThreadsSection + HeroNow)
  //   yesterday  → ['my-day', 'yesterday', tzOffsetMinutes]  (YesterdaySection)
  //   intent     → ['intent', 'today']  (IntentSection)
  //   journal    → ['journalEntry', today]  (EndOfDaySection)
  //
  // Sections that self-fetch because the bundle can't satisfy them:
  //   ['my-day']               — bundle.tasks is MyDayTask[] only, not the full
  //                              MyDayResponse (missing cards_due, focus_hours,
  //                              project_pulse, recommendations). Self-fetch keeps.
  //   ['pulse-today']          — pulse assembly lives in paper_ingestion, not
  //                              learning_engine. Self-fetch keeps.
  //   ['action-items-unprocessed'] — not in bundle. Self-fetch keeps.
  //   ['retention-stats']      — not in bundle. Self-fetch keeps.
  //   ['analytics', 'missing-foundational'] — not in bundle. Self-fetch keeps.
  //   ['digest-weekly']        — not in bundle. Self-fetch keeps.
  //   ['feed', 'reading', 'hero'] — not in bundle. Self-fetch keeps.
  //
  // Net result: ~4 section-level requests eliminated on cold mount.
  const { data: bundle } = useQuery<MyDayBundle>({
    queryKey: QUERY_KEYS.myDay.bundle(tzOffsetMinutes),
    queryFn: () => getMyDayBundle(),
    staleTime: 60_000, // bundle is a snapshot; individual queries may refresh faster
  });

  // Prime per-section caches when the bundle resolves — runs synchronously so
  // the section hooks find data immediately without a network call.
  useEffect(() => {
    if (!bundle) return;
    const today = todayIso();

    // threads → ThreadsSection + HeroNow both use ['my-day', 'threads']
    queryClient.setQueryData(QUERY_KEYS.myDay.threads(), bundle.threads);

    // yesterday — key includes tz offset to match YesterdaySection exactly
    queryClient.setQueryData(QUERY_KEYS.myDay.yesterday(tzOffsetMinutes), bundle.yesterday);

    // intent — matches IntentSection's ['intent', 'today']
    queryClient.setQueryData(QUERY_KEYS.intent.today(), bundle.intent);

    // journal — matches EndOfDaySection's ['journalEntry', today]
    queryClient.setQueryData(QUERY_KEYS.journal.entry(today), bundle.journal);
  }, [bundle, queryClient, tzOffsetMinutes]);

  useEffect(() => {
    if (!hash) return;
    const id = hash.slice(1);
    let frames = 0;
    let raf = 0;
    const tryScroll = () => {
      const el = document.getElementById(id);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      }
      if (++frames < 30) raf = requestAnimationFrame(tryScroll);
    };
    raf = requestAnimationFrame(tryScroll);
    return () => cancelAnimationFrame(raf);
  }, [hash]);

  return (
    <div className="bg-paper min-h-screen">
      <main className="max-w-page mx-auto px-4 py-6 sm:px-10 sm:py-10 space-y-8 sm:space-y-12">
        <DateMasthead />
        <YesterdaySection />
        <HeroNow />
        <IntentSection />
        <ProjectsSection />
        <ThreadsSection />
        <TodaysPulseSection />
        <TriageSection />
        <LearningFocusSection />
        <WeeklyDigestSection />
        <EndOfDaySection />
        <MyDayFooter entryNum={entryNum} />
      </main>
    </div>
  );
}
