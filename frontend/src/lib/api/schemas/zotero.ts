import { z } from 'zod';

export const zoteroTestSchema = z.looseObject({
  ok: z.boolean(),
  detail: z.string().optional(),
});

export const zoteroJobSchema = z.looseObject({
  job_id: z.string(),
  status: z.literal('queued'),
});

export const zoteroLinkageSchema = z.looseObject({
  paper_id: z.number(),
  zotero_item_key: z.string().nullable(),
  zotero_citation_key: z.string().nullable(),
  zotero_last_pushed_at: z.string().nullable(),
  zotero_library_type: z.enum(['user', 'group']).optional(),
  zotero_group_id: z.string().nullable().optional(),
});
