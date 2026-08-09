// Decks, cards, spaced-repetition review, card generation, and Anki export.
import { apiFetchJson, apiFetchRaw, apiFetchVoid, triggerBlobDownload } from './core';
import {
  cardSchema,
  deckSchema,
  generateCardsAcceptedSchema,
  retentionStatsSchema,
  reviewResponseSchema,
} from './schemas/cards';
import type {
  Deck,
  Card,
  ReviewResponse,
  RetentionStats,
  GenerateJobAccepted,
} from '@/types';

// --- Decks ---
export const fetchDecks = (): Promise<Deck[]> => apiFetchJson('/api/decks', deckSchema.array());
export const createDeck = (data: { name: string; description?: string | null }): Promise<Deck> =>
  apiFetchJson('/api/decks', deckSchema, { method: 'POST', body: JSON.stringify(data) });

// --- Cards ---
export const fetchCards = (deckId?: number): Promise<Card[]> =>
  apiFetchJson(`/api/cards${deckId ? `?deck_id=${deckId}` : ''}`, cardSchema.array());
export const createCard = (data: {
  deck_id: number;
  card_type: string;
  front: string;
  back: string;
  paper_id?: number | null;
}): Promise<Card> => apiFetchJson('/api/cards', cardSchema, { method: 'POST', body: JSON.stringify(data) });
export const updateCard = (id: number, data: Partial<Card>): Promise<Card> =>
  apiFetchJson(`/api/cards/${id}`, cardSchema, { method: 'PUT', body: JSON.stringify(data) });
export const deleteCard = (id: number) =>
  apiFetchVoid(`/api/cards/${id}`, { method: 'DELETE' });

// --- Review ---
export const getNextReview = (limit = 1, deckId?: number): Promise<Card[]> => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (deckId != null) params.append('deck_id', String(deckId));
  return apiFetchJson(`/api/review/next?${params.toString()}`, cardSchema.array());
};
export const submitReview = (cardId: number, rating: number, durationMs?: number): Promise<ReviewResponse> =>
  apiFetchJson(`/api/review/${cardId}`, reviewResponseSchema, {
    method: 'POST',
    body: JSON.stringify({ rating, review_duration_ms: durationMs ?? null }),
  });
export const getStats = (): Promise<RetentionStats> =>
  apiFetchJson('/api/stats', retentionStatsSchema);

// --- Generate & Export ---

/** Enqueue card generation for a single paper. Returns a job_id to poll. */
export const generateCardsJob = (paperId: number, deckId: number, maxCards = 5): Promise<GenerateJobAccepted> =>
  apiFetchJson('/api/generate', generateCardsAcceptedSchema, {
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
