import { z } from 'zod';
import type { JsonObject, JsonValue } from '@/types/json';

export type { JsonObject, JsonValue } from '@/types/json';

export const jsonValueSchema: z.ZodType<JsonValue> = z.lazy(() =>
  z.union([
    z.null(),
    z.boolean(),
    z.number(),
    z.string(),
    z.array(jsonValueSchema),
    z.record(z.string(), jsonValueSchema),
  ]),
);

export const jsonObjectSchema: z.ZodType<JsonObject> = z.record(
  z.string(),
  jsonValueSchema,
);

export const okResponseSchema = z.looseObject({ ok: z.boolean() });

export const apiErrorDetailSchema = z.looseObject({
  detail: jsonValueSchema.optional(),
});
