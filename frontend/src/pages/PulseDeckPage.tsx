import { PulseDeck } from '@/components/my-day/PulseDeck';
import { MarkerCaption } from '@/components/typography/MarkerCaption';

/**
 * /pulse — full Pulse Deck page.
 *
 * Wraps the existing PulseDeck widget (which renders ALL cards, header, generate
 * button, loading/error/empty states) in a page-level shell. The My Day page
 * shows a top-3 PulsePreviewCard that links here via "View all".
 *
 * Pulse deck page.
 */
export function PulseDeckPage() {
  return (
    <div className="space-y-6">
      <MarkerCaption marker="Today's Pulse" meta="ranked by relevance + your feedback" />
      <PulseDeck />
    </div>
  );
}
