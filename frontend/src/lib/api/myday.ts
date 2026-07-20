// Executive / My Day surface: quick tasks, focus logging, intent, journal,
// yesterday rollup, open threads, account self-service, weekly digest, and the
// aggregate My Day bundle.
import { apiFetch, ApiError } from './core';
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
  WeeklyDigestResponse,
} from '@/types';

// --- Executive / My Day ---

export const fetchMyDay = () =>
  apiFetch<MyDayResponse>('/api/executive/my-day');

export const createQuickTask = (data: { title: string; project_id?: number | null; priority?: number }) =>
  apiFetch<Task>('/api/executive/tasks', {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const logFocusSession = (data: { duration_hours: number; task_id?: number; paper_id?: number }) =>
  apiFetch<{ status: string; recorded_hours: number }>('/api/executive/focus/log', {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const fetchIntentToday = () =>
  apiFetch<{ intent: string | null; updated_at: string | null }>(
    '/api/executive/intent/today',
  );

export const saveIntentToday = (intent: string) =>
  apiFetch<{ intent: string | null; updated_at: string | null }>(
    '/api/executive/intent/today',
    { method: 'POST', body: JSON.stringify({ intent }) },
  );

// --- My Day Journal ---

export async function getJournalEntry(
  date: string,
  options?: { signal?: AbortSignal },
): Promise<JournalEntry | null> {
  try {
    return await apiFetch<JournalEntry>(
      `/api/my-day/journal?date=${encodeURIComponent(date)}`,
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
  return apiFetch<JournalEntry>('/api/my-day/journal', {
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
export const fetchYesterday = (tzOffsetMinutes = 0) =>
  apiFetch<YesterdaySummary>(
    `/api/my-day/yesterday?tz_offset_minutes=${tzOffsetMinutes}`,
  );

// --- My Day § Open threads (`thread` entity) ---

export const fetchThreads = () =>
  apiFetch<Thread[]>('/api/my-day/threads');
export const fetchThread = (threadId: number) =>
  apiFetch<Thread>(`/api/my-day/threads/${threadId}`);
export const createThread = (data: {
  title: string;
  anchor?: string | null;
  progress?: number;
}) =>
  apiFetch<Thread>('/api/my-day/threads', {
    method: 'POST',
    body: JSON.stringify(data),
  });
export const updateThread = (
  threadId: number,
  data: { title?: string; anchor?: string | null; progress?: number; status?: string },
) =>
  apiFetch<Thread>(`/api/my-day/threads/${threadId}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
/** The prototype's `resume →` action — bumps last_at and returns the thread. */
export const resumeThread = (threadId: number) =>
  apiFetch<Thread>(`/api/my-day/threads/${threadId}/resume`, { method: 'POST' });
/** Auto-seed producer 1 — interrupted Pomodoro session → thread. */
export const seedThreadFromPomodoro = (data: {
  title: string;
  anchor?: string | null;
  progress?: number;
}) =>
  apiFetch<ThreadSeedResponse>('/api/my-day/threads/seed/pomodoro', {
    method: 'POST',
    body: JSON.stringify(data),
  });
/** Auto-seed producer 2 — EOD "make this a thread" → thread. */
export const seedThreadFromEod = (data: {
  title: string;
  anchor?: string | null;
  progress?: number;
}) =>
  apiFetch<ThreadSeedResponse>('/api/my-day/threads/seed/eod', {
    method: 'POST',
    body: JSON.stringify(data),
  });

// --- Account (self-service profile) ---

export const fetchAccount = () => apiFetch<AccountResponse>('/api/account');
/**
 * PATCH /api/account — `display_name` applies immediately; an `email` change
 * is never silent (issues a verification link to the new address).
 */
export const updateAccount = (data: { display_name?: string | null; email?: string }) =>
  apiFetch<AccountUpdateResponse>('/api/account', {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
/** Consume the email-change token (mirrors /api/auth/verify). */
export const confirmEmailChange = (token: string) =>
  apiFetch<AccountResponse>('/api/account/confirm-email', {
    method: 'POST',
    body: JSON.stringify({ token }),
  });

// --- Weekly Digest ---

export async function fetchWeeklyDigest(days: number = 7): Promise<WeeklyDigestResponse> {
  return apiFetch<WeeklyDigestResponse>(`/api/digest/weekly?days=${days}`);
}

// --- Executive § My Day aggregate bundle ---

export const getMyDayBundle = () =>
  apiFetch<MyDayBundle>('/api/executive/my-day-bundle');
