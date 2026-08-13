// Executive / My Day surface: quick tasks, focus logging, intent, journal,
// yesterday rollup, open threads, account self-service, weekly digest, and the
// aggregate My Day bundle.
import { apiFetchJson, ApiError } from './core';
import {
  accountSchema,
  accountUpdateResponseSchema,
  activeFocusSessionSchema,
  focusSessionResponseSchema,
  focusSessionTransitionSchema,
  intentSchema,
  journalEntrySchema,
  myDayBundleSchema,
  myDayResponseSchema,
  taskSchema,
  threadSchema,
  threadSeedResponseSchema,
  weeklyDigestResponseSchema,
  yesterdaySummarySchema,
} from './schemas/myday';
import type {
  Task,
  MyDayResponse,
  MyDayBundle,
  JournalEntry,
  JournalPrompts,
  YesterdaySummary,
  Thread,
  ThreadSeedResponse,
  AccountResponse,
  AccountUpdateResponse,
  ActiveFocusSession,
  FocusSessionTransition,
  WeeklyDigestResponse,
} from '@/types';

// --- Executive / My Day ---

export const fetchMyDay = (): Promise<MyDayResponse> =>
  apiFetchJson('/api/executive/my-day', myDayResponseSchema);

export const createQuickTask = (data: { title: string; project_id?: number | null; priority?: number }): Promise<Task> =>
  apiFetchJson('/api/executive/tasks', taskSchema, {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const logFocusSession = (data: { duration_hours: number; task_id?: number; paper_id?: number }): Promise<{ status: 'success'; recorded_hours: number }> =>
  apiFetchJson('/api/executive/focus/log', focusSessionResponseSchema, {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const fetchActiveFocusSession = (): Promise<ActiveFocusSession | null> =>
  apiFetchJson('/api/executive/focus/active', activeFocusSessionSchema.nullable());

export const startFocusSession = (data: {
  duration_seconds: number;
  source: 'web';
  task_id?: number;
  paper_id?: number;
}): Promise<ActiveFocusSession> =>
  apiFetchJson('/api/executive/focus/start', activeFocusSessionSchema, {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const pauseFocusSession = (sessionId: number): Promise<FocusSessionTransition> =>
  apiFetchJson(
    `/api/executive/focus/${sessionId}/pause`,
    focusSessionTransitionSchema,
    { method: 'POST' },
  );

export const resumeFocusSession = (sessionId: number): Promise<FocusSessionTransition> =>
  apiFetchJson(
    `/api/executive/focus/${sessionId}/resume`,
    focusSessionTransitionSchema,
    { method: 'POST' },
  );

export const completeFocusSession = (
  sessionId: number,
  mode: 'elapsed' | 'stop',
): Promise<FocusSessionTransition> =>
  apiFetchJson(
    `/api/executive/focus/${sessionId}/complete`,
    focusSessionTransitionSchema,
    { method: 'POST', body: JSON.stringify({ mode }) },
  );

export const fetchIntentToday = (): Promise<{ intent: string | null; updated_at: string | null }> =>
  apiFetchJson(
    '/api/executive/intent/today',
    intentSchema,
  );

export const saveIntentToday = (intent: string): Promise<{ intent: string | null; updated_at: string | null }> =>
  apiFetchJson(
    '/api/executive/intent/today',
    intentSchema,
    { method: 'POST', body: JSON.stringify({ intent }) },
  );

// --- My Day Journal ---

export async function getJournalEntry(
  date: string,
  options?: { signal?: AbortSignal },
): Promise<JournalEntry | null> {
  try {
    return await apiFetchJson(
      `/api/my-day/journal?date=${encodeURIComponent(date)}`,
      journalEntrySchema.nullable(),
      { signal: options?.signal },
    );
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export async function upsertJournalEntry(
  date: string,
  prompts: JournalPrompts,
  signal?: AbortSignal,
): Promise<JournalEntry> {
  return apiFetchJson('/api/my-day/journal', journalEntrySchema, {
    method: 'POST',
    body: JSON.stringify({ date, prompts }),
    signal,
  });
}

// --- My Day § Yesterday (on-the-fly rollup) ---

/**
 * GET /api/my-day/yesterday — on-the-fly § Yesterday rollup.
 * `tzOffsetMinutes` = minutes EAST of UTC (JS `-new Date().getTimezoneOffset()`);
 * the server stores no per-user timezone so the client supplies it.
 */
export const fetchYesterday = (tzOffsetMinutes = 0): Promise<YesterdaySummary> =>
  apiFetchJson(
    `/api/my-day/yesterday?tz_offset_minutes=${tzOffsetMinutes}`,
    yesterdaySummarySchema,
  );

// --- My Day § Open threads (`thread` entity) ---

export const fetchThreads = (): Promise<Thread[]> =>
  apiFetchJson('/api/my-day/threads', threadSchema.array());
export const fetchThread = (threadId: number): Promise<Thread> =>
  apiFetchJson(`/api/my-day/threads/${threadId}`, threadSchema);
export const createThread = (data: {
  title: string;
  anchor?: string | null;
  progress?: number;
}): Promise<Thread> =>
  apiFetchJson('/api/my-day/threads', threadSchema, {
    method: 'POST',
    body: JSON.stringify(data),
  });
export const updateThread = (
  threadId: number,
  data: { title?: string; anchor?: string | null; progress?: number; status?: string },
): Promise<Thread> =>
  apiFetchJson(`/api/my-day/threads/${threadId}`, threadSchema, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
/** The prototype's `resume →` action — bumps last_at and returns the thread. */
export const resumeThread = (threadId: number): Promise<Thread> =>
  apiFetchJson(`/api/my-day/threads/${threadId}/resume`, threadSchema, { method: 'POST' });
/** Auto-seed producer 1 — interrupted Pomodoro session → thread. */
export const seedThreadFromPomodoro = (data: {
  title: string;
  anchor?: string | null;
  progress?: number;
}): Promise<ThreadSeedResponse> =>
  apiFetchJson('/api/my-day/threads/seed/pomodoro', threadSeedResponseSchema, {
    method: 'POST',
    body: JSON.stringify(data),
  });
/** Auto-seed producer 2 — EOD "make this a thread" → thread. */
export const seedThreadFromEod = (data: {
  title: string;
  anchor?: string | null;
  progress?: number;
}): Promise<ThreadSeedResponse> =>
  apiFetchJson('/api/my-day/threads/seed/eod', threadSeedResponseSchema, {
    method: 'POST',
    body: JSON.stringify(data),
  });

// --- Account (self-service profile) ---

export const fetchAccount = (): Promise<AccountResponse> =>
  apiFetchJson('/api/account', accountSchema);
/**
 * PATCH /api/account — `display_name` applies immediately; an `email` change
 * is never silent (issues a verification link to the new address).
 */
export const updateAccount = (data: { display_name?: string | null; email?: string }): Promise<AccountUpdateResponse> =>
  apiFetchJson('/api/account', accountUpdateResponseSchema, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
/** Consume the email-change token (mirrors /api/auth/verify). */
export const confirmEmailChange = (token: string): Promise<AccountResponse> =>
  apiFetchJson('/api/account/confirm-email', accountSchema, {
    method: 'POST',
    body: JSON.stringify({ token }),
  });

// --- Weekly Digest ---

export async function fetchWeeklyDigest(days: number = 7): Promise<WeeklyDigestResponse> {
  return apiFetchJson(`/api/digest/weekly?days=${days}`, weeklyDigestResponseSchema);
}

// --- Executive § My Day aggregate bundle ---

export const getMyDayBundle = (): Promise<MyDayBundle> =>
  apiFetchJson('/api/executive/my-day-bundle', myDayBundleSchema);
