import type { PriorityLevel } from '@/types';

export const PRIORITY_LABELS: Record<PriorityLevel, string> = {
  'must-read': 'Must read',
  recommended: 'Recommended',
  background: 'Background',
  unscored: 'Not yet ranked',
};
