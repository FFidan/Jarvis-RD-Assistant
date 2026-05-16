import { useQuery } from '@tanstack/react-query';
import { fetchTasks } from '@/lib/api';

export function useProjectTasks(projectId: number) {
  return useQuery({
    queryKey: ['tasks', projectId],
    queryFn: () => fetchTasks(projectId),
    enabled: projectId > 0,
  });
}
