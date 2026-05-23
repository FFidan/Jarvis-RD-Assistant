import { useQuery } from '@tanstack/react-query';
import { fetchTasks } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';

/**
 * Fetch tasks for a project.
 * Wraps the `['tasks', projectId]` query key from the central registry.
 *
 * This is the registry-backed equivalent of `use-project-tasks.ts`; new
 * call-sites should prefer this hook. Existing `useProjectTasks` consumers
 * are migrated lazily under the rot-on-touch policy (DRY-F2).
 */
export function useTasks(projectId: number) {
  return useQuery({
    queryKey: QUERY_KEYS.tasks.byProject(projectId),
    queryFn: () => fetchTasks(projectId),
    enabled: projectId > 0,
  });
}
