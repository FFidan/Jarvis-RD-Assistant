import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchProjectQuestions } from '@/lib/api';

export function useProjectQuestions(projectId: number) {
  return useQuery({
    queryKey: QUERY_KEYS.projects.questions(projectId),
    queryFn: () => fetchProjectQuestions(projectId),
    enabled: projectId > 0,
  });
}
