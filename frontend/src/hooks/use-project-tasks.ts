import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchTasks } from '@/lib/api';

export function useProjectTasks(projectId: number) {
  return useQuery({
    queryKey: QUERY_KEYS.tasks.byProject(projectId),
    queryFn: () => fetchTasks(projectId),
    enabled: projectId > 0,
  });
}
