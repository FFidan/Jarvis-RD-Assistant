import { QUERY_KEYS } from '@/lib/query-keys';
import type { useQueryClient } from '@tanstack/react-query';
import type { FirstRunStatus } from '@/lib/api';

export type StepKind =
  | 'welcome'
  | 'smtp'
  | 'admin'
  | 'cloud'
  | 'topic'
  | 'automation'
  | 'sources'
  | 'telegram'
  | 'done';

export const ALL_STEPS: readonly StepKind[] = [
  'welcome',
  'smtp',
  'admin',
  'cloud',
  'topic',
  'automation',
  'sources',
  'telegram',
  'done',
];

export const SINGLE_USER_FIRST_RUN_STEPS: readonly StepKind[] = [
  'welcome',
  'admin',
  'smtp',
  'cloud',
  'topic',
  'automation',
  'sources',
  'telegram',
  'done',
];

export const SETUP_TOKEN_STORAGE_KEY = 'jarvis_setup_token';

export function readStoredSetupToken(): string | null {
  try {
    return sessionStorage.getItem(SETUP_TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function storeSetupToken(token: string): void {
  try {
    sessionStorage.setItem(SETUP_TOKEN_STORAGE_KEY, token);
  } catch {
    /* sessionStorage unavailable (private mode / disabled) — degrade silently */
  }
}

export function clearStoredSetupToken(): void {
  try {
    sessionStorage.removeItem(SETUP_TOKEN_STORAGE_KEY);
  } catch {
    /* sessionStorage unavailable — nothing to clear */
  }
}

export interface StepNavProps {
  stepNumber: number;
  totalSteps: number;
  onBack?: () => void;
  onNext?: () => void;
}

export function markFirstRunCompleted(queryClient: ReturnType<typeof useQueryClient>): void {
  // Setup is finished — the one-time bootstrap token must not linger (M2).
  clearStoredSetupToken();
  queryClient.setQueryData<FirstRunStatus>(QUERY_KEYS.setup.firstRun(), (prev) =>
    prev ? { ...prev, setup_completed: true } : prev,
  );
  queryClient.invalidateQueries({ queryKey: QUERY_KEYS.setup.status() });
}
