import { useQuery } from '@tanstack/react-query';
import { fetchProjectActivity } from '@/lib/api';

export function useProjectActivity(projectId: number, limit?: number) {
  return useQuery({
    queryKey: ['project-activity', projectId],
    queryFn: () => fetchProjectActivity(projectId, limit),
    enabled: projectId > 0,
  });
}
