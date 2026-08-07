/**
 * LearningCardsPage — v3 IA: focused review session shell + Library view.
 *
 * Two destinations within /cards:
 *   Review session  (default when due_now > 0, or ?mode=review)
 *   Library         (?mode=library, or breadcrumb "Flashcards" link)
 *
 * Route default: shows Review if due_now > 0; Library otherwise.
 *
 * P2 OFFLINE SEAM — WIRED:
 * The `submitReviewFn` threaded into <ReviewMode> is the single boundary for
 * the submit-review call. `useOfflineReviewQueue()` returns an outbox-backed
 * submit fn that: ONLINE → delegates to the real `submitReview` UNCHANGED;
 * OFFLINE → appends to an IndexedDB outbox + resolves so the session advances;
 * on offline→online → drains the outbox idempotently and shows a single
 * reconcile toast. No other file needed changing — the seam is here.
 * See the Offline/PWA behavior contract.
 */

import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Plus, Sparkles, CloudOff } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { getStats, fetchDecks } from '@/lib/api';
import { useOnlineStatus } from '@/hooks/use-online-status';
import { useOfflineReviewQueue } from '@/components/cards/use-offline-review-queue';
import { Button } from '@/components/ui/button';
import { StatsHeader } from '@/components/cards/StatsHeader';
import { ReviewMode } from '@/components/cards/ReviewMode';
import { DeckBrowser } from '@/components/cards/DeckBrowser';
import { CardList } from '@/components/cards/CardList';
import { CreateCardForm, GenerateCardsDialog } from '@/components/cards/CreateCardForm';
import {
  SessionBreadcrumb,
  SessionProgressBar,
  useSessionProgress,
} from '@/components/cards/SessionShell';
import { SessionComplete } from '@/components/cards/SessionComplete';
import { QueryErrorState } from '@/components/shared/QueryErrorState';

type Mode = 'review' | 'library';

export function LearningCardsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [selectedDeckId, setSelectedDeckId] = useState<number | null>(null);
  const [showCreateCard, setShowCreateCard] = useState(false);
  const [showGenerate, setShowGenerate] = useState(false);
  const [sessionDeckId, setSessionDeckId] = useState<number | null>(null);
  const [sessionEnded, setSessionEnded] = useState(false);

  // P2 OFFLINE SEAM: outbox-backed submit fn (online → real submitReview
  // unchanged; offline → enqueue + optimistic advance; reconnect → drain).
  const { submitReviewFn } = useOfflineReviewQueue();
  const { online } = useOnlineStatus();

  // Fetch stats to determine default mode (review if due_now > 0).
  const { data: stats } = useQuery({
    queryKey: QUERY_KEYS.cards.stats(),
    queryFn: getStats,
    refetchInterval: 30_000,
  });

  // Fetch decks for breadcrumb deck-name resolution.
  const { data: decks = [], isError: decksError } = useQuery({
    queryKey: QUERY_KEYS.decks.list(),
    queryFn: fetchDecks,
  });

  // Resolve effective mode: URL param → stats-driven default.
  const paramMode = searchParams.get('mode') as Mode | null;
  const defaultMode: Mode =
    paramMode === 'library' ? 'library'
    : paramMode === 'review' ? 'review'
    : stats && stats.due_now === 0 ? 'library'
    : 'review';

  const [mode, setMode] = useState<Mode>(defaultMode);

  // Sync mode when stats arrive and no explicit param is set.
  useEffect(() => {
    if (!paramMode && stats) {
      setMode(stats.due_now === 0 ? 'library' : 'review');
    }
  }, [stats, paramMode]);

  // Session progress (client-side; resets when session starts).
  const sessionTotal = sessionDeckId != null
    ? (decks.find((d) => d.id === sessionDeckId)?.due_count ?? stats?.due_now ?? 0)
    : (stats?.due_now ?? 0);

  const { sessionReviewed, onReviewSuccess, sessionTotal: frozenTotal } =
    useSessionProgress(sessionTotal); // frozenTotal: seeded from first non-zero due count

  const deckName = sessionDeckId != null
    ? (decks.find((d) => d.id === sessionDeckId)?.name ?? null)
    : null;

  const navigateToLibrary = useCallback(() => {
    setMode('library');
    setSearchParams({ mode: 'library' });
    setSessionEnded(false);
  }, [setSearchParams]);

  const navigateToReview = useCallback(() => {
    setSessionDeckId(null);
    setMode('review');
    setSearchParams({});
    setSessionEnded(false);
  }, [setSearchParams]);

  const handleStartReview = useCallback(
    (deckId: number) => {
      setSessionDeckId(deckId);
      setSessionEnded(false);
      setMode('review');
      setSearchParams({});
    },
    [setSearchParams],
  );

  const handleSessionEnd = useCallback(() => {
    setSessionEnded(true);
  }, []);

  // ── Library view ──────────────────────────────────────────────────────────
  if (mode === 'library') {
    return (
      <div className="space-y-6">
        {/* Library header */}
        <div className="flex items-center justify-between">
          <h1 className="text-[32px] leading-tight tracking-tight text-strong">
            Flashcards
          </h1>
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => setShowGenerate(true)}>
              <Sparkles className="mr-1 h-4 w-4" /> Generate
            </Button>
            <Button onClick={() => setShowCreateCard(true)}>
              <Plus className="mr-1 h-4 w-4" /> New Card
            </Button>
            {stats && stats.due_now > 0 && (
              <Button variant="default" onClick={navigateToReview}>
                Review now ({stats.due_now})
              </Button>
            )}
          </div>
        </div>

        {/* Stats bar lives in Library view */}
        <StatsHeader />

        {/* Deck browser with "Start review" per deck */}
        <DeckBrowser
          selectedDeckId={selectedDeckId}
          onSelectDeck={setSelectedDeckId}
          onStartReview={handleStartReview}
        />

        {selectedDeckId && (
          <div className="space-y-3">
            <CardList deckId={selectedDeckId} />
          </div>
        )}

        <CreateCardForm
          open={showCreateCard}
          onOpenChange={setShowCreateCard}
          defaultDeckId={selectedDeckId}
        />
        <GenerateCardsDialog
          open={showGenerate}
          onOpenChange={setShowGenerate}
          defaultDeckId={selectedDeckId}
        />
      </div>
    );
  }

  // ── Review session view ───────────────────────────────────────────────────
  return (
    <div className="space-y-6">
      {/* Breadcrumb */}
      <SessionBreadcrumb
        deckName={deckName}
        onNavigateToLibrary={navigateToLibrary}
      />
      {decksError && <QueryErrorState message="Failed to load deck names." />}

      {/* Subtle offline state — reviews are queued, not lost */}
      {!online && !sessionEnded && (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <CloudOff className="h-3.5 w-3.5" />
          <span>Offline — ratings are queued and will sync when you&rsquo;re back online.</span>
        </div>
      )}

      {/* Progress bar — hidden when session complete (no more cards) */}
      {!sessionEnded && (
        <SessionProgressBar
          reviewed={sessionReviewed}
          total={frozenTotal}
        />
      )}

      {/* Card canvas or session-complete panel */}
      {sessionEnded ? (
        <SessionComplete
          sessionReviewed={sessionReviewed}
          onNavigateToLibrary={navigateToLibrary}
        />
      ) : (
        <ReviewMode
          sessionCardIndex={sessionReviewed + 1}
          deckId={sessionDeckId}
          submitReviewFn={submitReviewFn}   // P2 OFFLINE SEAM: outbox-backed (online unchanged)
          onReviewSuccess={onReviewSuccess}
          onSessionEnd={handleSessionEnd}
        />
      )}
    </div>
  );
}
