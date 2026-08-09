import { z } from 'zod';
import { jsonObjectSchema } from './common';

export const evidenceSchema = z.looseObject({
  quote: z.string().nullable(),
  page_number: z.number().nullable(),
  chunk_id: z.number().nullable(),
  snapshot_path: z.string().nullable(),
  verified: z.boolean(),
});

export const deckSchema = z.looseObject({
  id: z.number(),
  name: z.string(),
  description: z.string().nullable(),
  topic_id: z.number().nullable(),
  card_count: z.number(),
  due_count: z.number(),
  created_at: z.string(),
});

export const cardSchema = z.looseObject({
  id: z.number(),
  deck_id: z.number(),
  paper_id: z.number().nullable(),
  card_type: z.enum(['concept', 'quote', 'method', 'comparison']),
  front: z.string(),
  back: z.string(),
  evidence: evidenceSchema.nullable(),
  fsrs_state: jsonObjectSchema,
  due_at: z.string().nullable(),
  stale: z.boolean(),
  created_at: z.string(),
  updated_at: z.string(),
});

export const reviewResponseSchema = z.looseObject({
  card_id: z.number(),
  rating: z.number(),
  next_due_at: z.string(),
  fsrs_state: jsonObjectSchema,
  review_log_id: z.number(),
});

export const retentionStatsSchema = z.looseObject({
  total_cards: z.number(),
  due_now: z.number(),
  reviewed_today: z.number(),
  average_retention: z.number(),
  reviews_by_rating: z.record(z.string(), z.number()),
  streak_days: z.number(),
});

export const generateCardsAcceptedSchema = z.looseObject({
  job_id: z.string(),
  status: z.literal('queued'),
});
