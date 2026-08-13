import { z } from 'zod';
import { jsonObjectSchema, jsonValueSchema } from './common';

export const jobStatusSchema = z.enum([
  'queued',
  'running',
  'succeeded',
  'failed',
  'cancelled',
]);

export const jobErrorSchema = z.looseObject({
  message: z.string(),
  action_link: z.looseObject({
    label: z.string(),
    href: z.string(),
  }).optional(),
});

const nonNegativeIntegerSchema = z.number().int().nonnegative();

/**
 * Job results are additive JSON objects, but the fields consumed by shared UI
 * effects must retain their exact runtime types.
 */
export const jobResultSchema = z.object({
  status: z.string().optional(),
  cards_created: nonNegativeIntegerSchema.optional(),
  coverage: z.number().finite().min(0).max(1).optional(),
  passes: nonNegativeIntegerSchema.optional(),
  total: nonNegativeIntegerSchema.optional(),
  remaining: nonNegativeIntegerSchema.optional(),
  failed: nonNegativeIntegerSchema.optional(),
  skipped: nonNegativeIntegerSchema.optional(),
  errors: z.array(jsonValueSchema).optional(),
  blocked: z.array(jsonValueSchema).optional(),
}).catchall(jsonValueSchema);

export const jobSchema = z.looseObject({
  id: z.string(),
  kind: z.string(),
  status: jobStatusSchema,
  cancel_requested: z.boolean().optional(),
  progress: z.number().nullable(),
  progress_message: z.string().nullable(),
  payload: jsonObjectSchema.nullable().optional(),
  result: jobResultSchema.nullable(),
  error: jobErrorSchema.nullable(),
  created_at: z.string().nullable(),
  updated_at: z.string().nullable().optional(),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
});

const jobStreamUpdateSchema = z.object({
  status: jobStatusSchema,
  cancel_requested: z.boolean().optional(),
  progress: z.number().finite().nullable().optional(),
  progress_message: z.string().nullable().optional(),
  payload: jsonObjectSchema.nullable().optional(),
  result: jobResultSchema.nullable().optional(),
  error: jobErrorSchema.nullable().optional(),
});

export const jobStreamEventSchema = z.union([
  z.object({ status: z.literal('streaming_timeout') }),
  z.object({ error: z.literal('status_unavailable') }),
  jobStreamUpdateSchema,
]);

export const createJobResponseSchema = z.looseObject({
  job_id: z.string(),
  status: z.literal('queued'),
  reason: z.null().optional(),
});

export type DecodedJob = z.infer<typeof jobSchema>;
export type JobResult = z.infer<typeof jobResultSchema>;
