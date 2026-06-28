import { type PriorityLevel } from '@/types';

export function priorityLevel(score: number | null): PriorityLevel {
  if (score === null) return 'unscored';
  if (score > 0.7) return 'must-read';
  if (score > 0.4) return 'recommended';
  return 'background';
}
