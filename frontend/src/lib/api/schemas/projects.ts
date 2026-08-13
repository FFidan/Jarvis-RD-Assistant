import { z } from 'zod';

export const projectStatusSchema = z.enum(['active', 'paused', 'completed', 'archived']);
export const taskStatusSchema = z.enum(['todo', 'in_progress', 'done', 'blocked']);

export const projectSchema = z.looseObject({
  id: z.number(),
  name: z.string(),
  description: z.string().nullable(),
  status: projectStatusSchema,
  deadline: z.string().nullable(),
  color: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
  paper_count: z.number().optional(),
  open_question_count: z.number().optional(),
});

export const projectQuestionSchema = z.looseObject({
  id: z.number(),
  project_id: z.number(),
  body: z.string(),
  created_at: z.string(),
});

export const projectActivitySchema = z.looseObject({
  kind: z.enum(['added_paper', 'completed_task', 'completed_milestone']),
  ts: z.string(),
  label: z.string(),
});

export const taskSchema = z.looseObject({
  id: z.number(),
  project_id: z.number().nullable(),
  parent_task_id: z.number().nullable(),
  title: z.string(),
  description: z.string().nullable(),
  status: taskStatusSchema,
  priority: z.number(),
  deadline: z.string().nullable(),
  estimated_hours: z.number().nullable(),
  actual_hours: z.number().nullable(),
  sort_order: z.number(),
  completed_at: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
  project_name: z.string().nullable().optional(),
});

export const milestoneSchema = z.looseObject({
  id: z.number(),
  project_id: z.number(),
  name: z.string(),
  deadline: z.string().nullable(),
  description: z.string().nullable(),
  completed: z.boolean(),
  completed_at: z.string().nullable(),
  created_at: z.string(),
});

export const projectPaperSchema = z.looseObject({
  id: z.number(),
  title: z.string(),
  authors: z.array(z.string()),
  source_type: z.string(),
  published_date: z.string().nullable(),
  notes: z.string().nullable(),
  added_at: z.string(),
});

export const projectPaperLinkSchema = z.looseObject({
  project_id: z.number(),
  paper_id: z.number(),
});

export const librarySearchPaperSchema = z.looseObject({
  id: z.number(),
  title: z.string(),
  published_date: z.string().nullable(),
});

export type LibrarySearchPaper = z.infer<typeof librarySearchPaperSchema>;
