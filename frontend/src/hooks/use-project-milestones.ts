import { useQuery } from '@tanstack/react-query';
import { fetchMilestones } from '@/lib/api';

export function useProjectMilestones(projectId: number) {
  return useQuery({
    queryKey: ['milestones', projectId],
    queryFn: () => fetchMilestones(projectId),
    enabled: projectId > 0,
  });
}
