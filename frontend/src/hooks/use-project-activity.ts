import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchProjectActivity } from '@/lib/api';

export function useProjectActivity(projectId: number, limit?: number) {
  return useQuery({
    queryKey: QUERY_KEYS.projects.activity(projectId),
    queryFn: () => fetchProjectActivity(projectId, limit),
    enabled: projectId > 0,
  });
}
