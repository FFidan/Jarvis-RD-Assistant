import { z } from 'zod';
import { jsonObjectSchema } from './common';

export const systemEventSchema = z.looseObject({
  id: z.number(),
  created_at: z.string(),
  level: z.enum(['debug', 'info', 'warning', 'error', 'critical']),
  category: z.enum(['error', 'job', 'source', 'auth', 'config', 'infra']),
  source: z.string(),
  message: z.string(),
  context: jsonObjectSchema,
  correlation_id: z.string().nullable(),
});

export const logsListResponseSchema = z.looseObject({
  events: z.array(systemEventSchema),
  next_cursor: z.number().nullable(),
});

export const logsSummarySchema = z.looseObject({
  by_level: z.record(z.string(), z.number()),
  by_category: z.record(z.string(), z.number()),
  total: z.number(),
});
