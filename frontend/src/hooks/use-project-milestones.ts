import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchMilestones } from '@/lib/api';

export function useProjectMilestones(projectId: number) {
  return useQuery({
    queryKey: QUERY_KEYS.projects.milestones(projectId),
    queryFn: () => fetchMilestones(projectId),
    enabled: projectId > 0,
  });
}
