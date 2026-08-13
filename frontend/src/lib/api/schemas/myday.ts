import { z } from 'zod';
import { taskSchema, taskStatusSchema } from './projects';

export const myDayTaskSchema = z.looseObject({
  id: z.number(),
  project_id: z.number().nullable(),
  title: z.string(),
  priority: z.number(),
  deadline: z.string().nullable(),
  status: taskStatusSchema,
  completed_at: z.string().nullable(),
  project_name: z.string().nullable(),
  project_color: z.string().nullable(),
});

export const myDayResponseSchema = z.looseObject({
  tasks: z.array(myDayTaskSchema),
  cards_due: z.number(),
  recommendations: z.array(z.looseObject({
    recommendation_id: z.number(),
    paper_id: z.number(),
    score: z.number(),
    title: z.string(),
    authors: z.array(z.string()),
  })),
  today_focus_hours: z.number(),
  focus_streak_days: z.number(),
  project_pulse: z.array(z.looseObject({
    id: z.number(),
    name: z.string(),
    color: z.string().nullable(),
    total_tasks: z.number(),
    done_tasks: z.number(),
    next_milestone: z.string().nullable(),
    next_milestone_deadline: z.string().nullable(),
  })),
});

export const focusSessionResponseSchema = z.looseObject({
  status: z.literal('success'),
  recorded_hours: z.number(),
});

export const activeFocusSessionSchema = z.looseObject({
  id: z.number(),
  state: z.enum(['active', 'paused', 'completed']),
  source: z.enum(['web', 'telegram']),
  duration_seconds: z.number(),
  remaining_seconds: z.number(),
  started_at: z.string(),
  paused_at: z.string().nullable(),
  paused_seconds: z.number(),
  completed_at: z.string().nullable(),
  recorded_seconds: z.number(),
  task_id: z.number().nullable(),
  paper_id: z.number().nullable(),
});

export const focusSessionTransitionSchema = z.looseObject({
  session: activeFocusSessionSchema,
  changed: z.boolean(),
});

export const intentSchema = z.looseObject({
  intent: z.string().nullable(),
  updated_at: z.string().nullable(),
});

export const journalPromptsSchema = z.looseObject({
  first_move: z.string().nullable().optional(),
  worked: z.string().nullable().optional(),
  blocked: z.string().nullable().optional(),
  note: z.string().nullable().optional(),
});

export const journalEntrySchema = z.looseObject({
  id: z.number(),
  date: z.string(),
  prompts: journalPromptsSchema,
  created_at: z.string(),
  updated_at: z.string(),
});

export const yesterdayTaskSchema = z.looseObject({
  id: z.number(),
  title: z.string(),
  status: taskStatusSchema,
});

export const yesterdaySummarySchema = z.looseObject({
  date: z.string(),
  focused_hours: z.number(),
  cards_reviewed: z.number(),
  tasks_done: z.number(),
  completed: z.array(yesterdayTaskSchema),
  deferred: z.array(yesterdayTaskSchema),
});

export const threadSchema = z.looseObject({
  id: z.number(),
  title: z.string(),
  anchor: z.string().nullable(),
  progress: z.number(),
  last_at: z.string(),
  status: z.enum(['open', 'done', 'archived']),
  created_at: z.string(),
});

export const threadSeedResponseSchema = z.looseObject({
  thread: threadSchema,
  created: z.boolean(),
});

export const accountSchema = z.looseObject({
  id: z.number(),
  email: z.string(),
  role: z.enum(['user', 'admin']),
  display_name: z.string().nullable(),
  created_at: z.string(),
  last_login_at: z.string().nullable(),
});

export const accountUpdateResponseSchema = z.looseObject({
  account: accountSchema,
  email_verification_sent: z.boolean(),
});

export const weeklyDigestThemeSchema = z.looseObject({
  theme: z.string(),
  supporting_papers: z.array(z.number()),
  notes: z.string().nullable(),
  verified: z.boolean().nullable(),
  verification_reason: z.string().nullable(),
});

export const weeklyDigestResponseSchema = z.looseObject({
  topics: z.array(z.looseObject({
    name: z.string(),
    paper_count: z.number(),
    themes: z.array(weeklyDigestThemeSchema),
    top_papers: z.array(z.looseObject({
      id: z.number(),
      title: z.string(),
      url: z.string().nullable(),
      confidence: z.string().nullable(),
      relevance_score: z.number().nullable(),
    })),
    summary: z.string(),
  })),
  total_papers: z.number(),
  period_start: z.string(),
  period_end: z.string(),
});

export const myDayBundleSchema = z.looseObject({
  tasks: z.array(myDayTaskSchema),
  intent: intentSchema,
  threads: z.array(threadSchema.extend({ last_at: z.string().nullable() })),
  yesterday: yesterdaySummarySchema,
  journal: journalEntrySchema.nullable(),
});

export { taskSchema };
