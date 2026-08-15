import type { LifecycleState } from '@/types';

export const PAPER_STATE_LABELS: Record<LifecycleState, string> = {
  inbox: 'Inbox',
  to_read: 'Reading List',
  reading: 'Reading',
  done: 'Done',
  trash: 'Trash',
};

export function paperStateLabel(state: string): string {
  return Object.entries(PAPER_STATE_LABELS).find(([key]) => key === state)?.[1] ?? state;
}
