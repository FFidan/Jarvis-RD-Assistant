/**
 * SessionShell — breadcrumb + progress bar wrapper for the focused review session.
 *
 * Owns the client-side session state: sessionTotal (seed from due_now at route entry)
 * and sessionReviewed (incremented on each successful submitReview).
 *
 * P2 OFFLINE SEAM (Wave 3 / functional-track): the submit-review call is isolated
 * behind `onReviewSubmit` in ReviewCanvasProps. To add offline IndexedDB outbox +
 * sync, replace the implementation passed into <ReviewCanvas onReviewSubmit={...}>
 * without touching this shell or the canvas layout. The seam is intentional.
 * See docs/superpowers/specs/2026-05-15-learning-cards-ia-redesign-design.md §Offline.
 */

import { useCallback, useEffect, useState } from 'react';
import { ChevronRight } from 'lucide-react';
import type { Deck } from '@/types';

export interface SessionProgress {
  /** Number of cards reviewed in this session (client-side, resets on route visit). */
  sessionReviewed: number;
  /** Total cards due at session start (seed from RetentionStats.due_now). */
  sessionTotal: number;
  /** Call this after each successful submitReview mutation. */
  onReviewSuccess: () => void;
}

export function useSessionProgress(currentTotal: number): SessionProgress {
  const [sessionReviewed, setSessionReviewed] = useState(0);
  // sessionTotal is seeded once from the first non-zero currentTotal value,
  // then frozen for the remainder of the session (spec §5 Conflict C).
  const [frozenTotal, setFrozenTotal] = useState(0);

  useEffect(() => {
    if (frozenTotal === 0 && currentTotal > 0) {
      setFrozenTotal(currentTotal);
    }
  }, [currentTotal, frozenTotal]);

  const sessionTotal = frozenTotal > 0 ? frozenTotal : currentTotal;

  const onReviewSuccess = useCallback(() => {
    setSessionReviewed((n) => n + 1);
  }, []);

  return { sessionReviewed, sessionTotal, onReviewSuccess };
}

interface BreadcrumbProps {
  deckName: string | null;
  /** Callback when user clicks "Flashcards" to return to Library view. */
  onNavigateToLibrary: () => void;
}

export function SessionBreadcrumb({ deckName, onNavigateToLibrary }: BreadcrumbProps) {
  return (
    <nav
      aria-label="Breadcrumb"
      className="flex items-center gap-1 text-xs text-muted-foreground tracking-wide"
    >
      <span>Reflect</span>
      <ChevronRight className="h-3 w-3" />
      <button
        type="button"
        onClick={onNavigateToLibrary}
        className="hover:text-foreground transition-colors underline-offset-2 hover:underline"
      >
        Flashcards
      </button>
      <ChevronRight className="h-3 w-3" />
      <span className="text-foreground font-medium">
        {deckName ? `${deckName} · session` : 'All decks · session'}
      </span>
    </nav>
  );
}

interface SessionProgressBarProps {
  reviewed: number;
  total: number;
}

export function SessionProgressBar({ reviewed, total }: SessionProgressBarProps) {
  const pct = total > 0 ? Math.min(100, Math.round((reviewed / total) * 100)) : 0;

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold tracking-widest text-muted-foreground uppercase">
          Progress
        </span>
        <span className="text-xs text-muted-foreground tabular-nums">
          {reviewed} / {total > 0 ? total : '—'}
        </span>
      </div>
      <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden">
        <div
          className="h-1.5 rounded-full bg-[hsl(var(--ring))] transition-all duration-500 ease-out"
          style={{ width: `${pct}%` }}
          role="progressbar"
          aria-valuenow={reviewed}
          aria-valuemin={0}
          aria-valuemax={total}
          aria-label={`${reviewed} of ${total} cards reviewed`}
        />
      </div>
    </div>
  );
}

/** Resolve a deck name from the cached decks list by deck_id. */
export function resolveDeckName(decks: Deck[], deckId: number | null): string | null {
  if (deckId == null) return null;
  return decks.find((d) => d.id === deckId)?.name ?? null;
}

/** Compute "last seen N days" from card.updated_at (proxy for last review timestamp). */
export function computeLastSeenDays(updatedAt: string | null): number | null {
  if (!updatedAt) return null;
  const diff = Date.now() - new Date(updatedAt).getTime();
  return Math.max(0, Math.floor(diff / (1000 * 60 * 60 * 24)));
}
