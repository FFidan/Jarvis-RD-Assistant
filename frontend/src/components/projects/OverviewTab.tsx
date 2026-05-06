import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CalendarDays, CheckCircle2, ListTodo, Pencil, Target } from 'lucide-react';
import type { Project } from '@/types';
import { fetchTasks, updateProject } from '@/lib/api';
import { MetricTile } from '@/components/MetricTile';
import { Progress } from '@/components/ui/progress';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { SectionHeader } from '@/components/my-day/sections/SectionHeader';
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

  const deadlineColor = deadlineDays === null ? 'text-muted-foreground'
    : deadlineDays < 0 ? 'text-destructive'
    : deadlineDays <= 7 ? 'text-[hsl(var(--status-warn))]'
    : '';

  const deadlineLabel = deadlineDays === null ? 'No deadline'
    : deadlineDays < 0 ? `${-deadlineDays}d overdue`
    : deadlineDays === 0 ? 'Due today'
    : `${deadlineDays}d left`;

  function updateDeadline(deadline: string | null) {
    updateProject(project.id, { deadline });
    queryClient.invalidateQueries({ queryKey: ['projects'] });
    queryClient.invalidateQueries({ queryKey: ['project', project.id] });
  }

  return (
    <div className="space-y-6">
      <SectionHeader marker="OVERVIEW" />
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricTile title="Total Tasks" value={totalTasks} icon={ListTodo} />
        <MetricTile title="Done" value={doneTasks} icon={CheckCircle2} />
        <MetricTile title="Progress" value={`${progress}%`} icon={Target} />
        <Card className={`rounded-md border-hair shadow-none ${deadlineDays !== null && deadlineDays < 0 ? 'border-destructive/30' : ''}`}>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Deadline</CardTitle>
            <div className="flex items-center gap-1">
              <Popover>
                <PopoverTrigger asChild>
                  <Button variant="ghost" size="icon" className="h-6 w-6" aria-label="Edit deadline">
                    <Pencil className="h-3 w-3" />
                  </Button>
                </PopoverTrigger>
                <PopoverContent className="w-auto p-3 space-y-2">
                  <input
                    type="date"
                    className="block w-full text-sm border rounded px-2 py-1"
                    value={project.deadline ?? ''}
                    onChange={(e) => updateDeadline(e.target.value || null)}
                  />
                  {project.deadline && (
                    <Button variant="ghost" size="sm" className="w-full text-xs" onClick={() => updateDeadline(null)}>
                      Clear deadline
                    </Button>
                  )}
                </PopoverContent>
              </Popover>
              <CalendarDays className={cn('h-4 w-4', deadlineColor)} />
            </div>
          </CardHeader>
          <CardContent>
            <div className={cn('text-2xl font-bold', deadlineColor)}>{deadlineLabel}</div>
            {project.deadline && <p className="text-xs text-muted-foreground">{project.deadline}</p>}
          </CardContent>
        </Card>
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
