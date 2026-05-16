import { useQuery } from '@tanstack/react-query';
import { fetchProjectQuestions } from '@/lib/api';

export function useProjectQuestions(projectId: number) {
  return useQuery({
    queryKey: ['project-questions', projectId],
    queryFn: () => fetchProjectQuestions(projectId),
    enabled: projectId > 0,
  });
}
