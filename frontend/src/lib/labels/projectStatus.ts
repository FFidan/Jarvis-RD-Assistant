import type { ProjectStatus } from '@/types';

export const PROJECT_STATUS_LABELS: Record<ProjectStatus, string> = {
  active: 'In progress',
  paused: 'Draft',
  completed: 'Completed',
  archived: 'Archived',
};
