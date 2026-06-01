// Decks, cards, spaced-repetition review, card generation, and Anki export.
import { apiFetch, apiFetchRaw, triggerBlobDownload } from './core';
import type {
  Deck,
  Card,
  ReviewResponse,
  RetentionStats,
  GenerateJobAccepted,
} from '@/types';

// --- Decks ---
export const fetchDecks = () => apiFetch<Deck[]>('/api/decks');
export const createDeck = (data: { name: string; description?: string | null }) =>
  apiFetch<Deck>('/api/decks', { method: 'POST', body: JSON.stringify(data) });

// --- Cards ---
export const fetchCards = (deckId?: number) =>
  apiFetch<Card[]>(`/api/cards${deckId ? `?deck_id=${deckId}` : ''}`);
export const createCard = (data: {
  deck_id: number;
  card_type: string;
  front: string;
  back: string;
  paper_id?: number | null;
}) => apiFetch<Card>('/api/cards', { method: 'POST', body: JSON.stringify(data) });
export const updateCard = (id: number, data: Partial<Card>) =>
  apiFetch<Card>(`/api/cards/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteCard = (id: number) =>
  apiFetch<void>(`/api/cards/${id}`, { method: 'DELETE' });

// --- Review ---
export const getNextReview = (limit = 1, deckId?: number) => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (deckId != null) params.append('deck_id', String(deckId));
  return apiFetch<Card[]>(`/api/review/next?${params.toString()}`);
};
export const submitReview = (cardId: number, rating: number, durationMs?: number) =>
  apiFetch<ReviewResponse>(`/api/review/${cardId}`, {
    method: 'POST',
    body: JSON.stringify({ rating, review_duration_ms: durationMs ?? null }),
  });
export const getStats = () => apiFetch<RetentionStats>('/api/stats');

// --- Generate & Export ---

/** Enqueue card generation for a single paper. Returns a job_id to poll. */
export const generateCardsJob = (paperId: number, deckId: number, maxCards = 5) =>
  apiFetch<GenerateJobAccepted>('/api/generate', {
    method: 'POST',
    body: JSON.stringify({ paper_id: paperId, deck_id: deckId, max_cards: maxCards }),
  });

export async function exportAnki(deckId: number): Promise<void> {
  const res = await apiFetchRaw(`/api/export/anki/${deckId}`);
  const blob = await res.blob();
  const disposition = res.headers.get('Content-Disposition') || '';
  const match = disposition.match(/filename="?([^"]+)"?/);
  const filename = (match && match[1]) ? match[1] : `deck_${deckId}.apkg`;
  triggerBlobDownload(blob, filename);
}
