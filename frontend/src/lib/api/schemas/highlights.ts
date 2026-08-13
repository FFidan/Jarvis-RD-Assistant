import { z } from 'zod';

export const rectSchema = z.looseObject({
  x0: z.number(),
  y0: z.number(),
  x1: z.number(),
  y1: z.number(),
});

export const highlightRectSchema = z.looseObject({
  boundingRect: rectSchema,
  rects: z.array(rectSchema),
});

export const highlightSchema = z.looseObject({
  id: z.number(),
  paper_id: z.number(),
  page: z.number(),
  rect: highlightRectSchema,
  note: z.string().nullable(),
  color: z.string().nullable(),
  quote: z.string().nullable(),
  created_at: z.string(),
  stale: z.boolean(),
});
