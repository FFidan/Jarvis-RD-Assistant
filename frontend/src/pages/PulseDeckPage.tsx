import { PulseDeck } from '@/components/my-day/PulseDeck';

/**
 * /pulse — full Pulse Deck page.
 *
 * Wraps the existing PulseDeck widget (which renders ALL cards, header, generate
 * button, loading/error/empty states) in a page-level shell. The My Day page
 * shows a top-3 PulsePreviewCard that links here via "View all".
 *
 * Spec: docs/specs/2026-04-29-paper-lifecycle-redesign.md §5.4 + Amendment 7.
 */
export function PulseDeckPage() {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Today's Pulse</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Full deck of curated paper recommendations, ranked by relevance signals
          and your feedback history.
        </p>
      </header>
      <PulseDeck />
    </div>
  );
}
