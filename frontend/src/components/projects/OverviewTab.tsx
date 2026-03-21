import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CalendarDays, CheckCircle2, ListTodo, Target } from 'lucide-react';
import type { Project } from '@/types';
import { fetchTasks, updateProject } from '@/lib/api';
import { MetricTile } from '@/components/MetricTile';
import { Progress } from '@/components/ui/progress';
import { Badge } from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

const STATUSES = ['active', 'paused', 'completed', 'archived'] as const;

interface OverviewTabProps {
  project: Project;
}

export function OverviewTab({ project }: OverviewTabProps) {
  const queryClient = useQueryClient();

  const { data: tasks = [] } = useQuery({
    queryKey: ['tasks', project.id],
    queryFn: () => fetchTasks(project.id),
  });

  const updateMut = useMutation({
    mutationFn: (status: string) => updateProject(project.id, { status } as Partial<Project>),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['project', project.id] });
    },
  });

  const totalTasks = tasks.length;
  const doneTasks = tasks.filter((t) => t.status === 'done').length;
  const progress = totalTasks > 0 ? Math.round((doneTasks / totalTasks) * 100) : 0;

  const deadlineDays = project.deadline
    ? Math.ceil((new Date(project.deadline).getTime() - Date.now()) / (1000 * 60 * 60 * 24))
    : null;

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricTile title="Total Tasks" value={totalTasks} icon={ListTodo} />
        <MetricTile title="Done" value={doneTasks} icon={CheckCircle2} />
        <MetricTile title="Progress" value={`${progress}%`} icon={Target} />
        <MetricTile
          title="Deadline"
          value={deadlineDays !== null ? (deadlineDays >= 0 ? `${deadlineDays}d left` : `${-deadlineDays}d overdue`) : 'None'}
          icon={CalendarDays}
          subtitle={project.deadline ?? undefined}
        />
      </div>

      <div>
        <Progress value={progress} className="h-3" />
      </div>

      {project.description && (
        <div>
          <h3 className="text-sm font-medium text-muted-foreground mb-1">Description</h3>
          <p className="text-sm">{project.description}</p>
        </div>
      )}

      <div className="flex items-center gap-4">
        <div className="space-y-1">
          <h3 className="text-sm font-medium text-muted-foreground">Status</h3>
          <Select
            value={project.status}
            onValueChange={(val) => updateMut.mutate(val)}
          >
            <SelectTrigger className="w-[180px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STATUSES.map((s) => (
                <SelectItem key={s} value={s}>
                  <Badge variant="outline" className="capitalize">{s}</Badge>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1">
          <h3 className="text-sm font-medium text-muted-foreground">Created</h3>
          <p className="text-sm">{new Date(project.created_at).toLocaleDateString()}</p>
        </div>

        <div className="space-y-1">
          <h3 className="text-sm font-medium text-muted-foreground">Updated</h3>
          <p className="text-sm">{new Date(project.updated_at).toLocaleDateString()}</p>
        </div>
      </div>
    </div>
  );
}
