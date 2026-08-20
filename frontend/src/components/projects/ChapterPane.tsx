import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Trash2, FolderKanban, Pencil } from 'lucide-react';
import type { Project } from '@/types';
import { deleteProject, updateProject, fetchTasks, fetchMilestones, fetchProjectQuestions, fetchProjectActivity } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from '@/components/ui/select';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { EmptyState } from '@/components/EmptyState';
import { ErrorSentinel } from '@/components/shared/ErrorSentinel';
import { MilestonesTab } from './MilestonesTab';
import { TasksTab } from './TasksTab';
import { LinkedPapersTab } from './LinkedPapersTab';
import { QuestionsSection } from './QuestionsSection';
import { RecentActivitySection } from './RecentActivitySection';
import { PROJECT_STATUS_LABELS } from '@/lib/labels/projectStatus';
import { Link } from 'react-router-dom';

const STATUS_VARIANTS: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  active: 'default',
  paused: 'secondary',
  completed: 'outline',
  archived: 'secondary',
};

const BACKEND_STATUSES = ['active', 'paused', 'completed', 'archived'] as const;

interface ChapterPaneProps {
  project: Project | null;
  onDeleted: () => void;
}

export function ChapterPane({ project, onDeleted }: ChapterPaneProps) {
  const queryClient = useQueryClient();
  const [showDelete, setShowDelete] = useState(false);

  const { data: questions = [], isError: questionsError } = useQuery({
    queryKey: QUERY_KEYS.projects.questions(project?.id ?? 0),
    queryFn: () => fetchProjectQuestions(project?.id ?? 0),
    enabled: (project?.id ?? 0) > 0,
  });
  const { data: activityItems = [], isError: activityError } = useQuery({
    queryKey: QUERY_KEYS.projects.activity(project?.id ?? 0),
    queryFn: () => fetchProjectActivity(project?.id ?? 0),
    enabled: (project?.id ?? 0) > 0,
  });
  const { data: milestones = [] } = useQuery({
    queryKey: QUERY_KEYS.projects.milestones(project?.id ?? 0),
    queryFn: () => fetchMilestones(project?.id ?? 0),
    enabled: (project?.id ?? 0) > 0,
  });
  const { data: tasks = [] } = useQuery({
    queryKey: QUERY_KEYS.tasks.byProject(project?.id ?? 0),
    queryFn: () => fetchTasks(project?.id ?? 0),
    enabled: (project?.id ?? 0) > 0,
  });

  const deleteMut = useMutation({
    mutationFn: () => deleteProject(project!.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.projects.list() });
      setShowDelete(false);
      onDeleted();
    },
  });

  const statusMut = useMutation({
    mutationFn: (status: string) =>
      updateProject(project!.id, { status } as Partial<Project>),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.projects.list() });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.projects.detail(project!.id) });
    },
  });

  const deadlineMut = useMutation({
    mutationFn: (deadline: string | null) =>
      updateProject(project!.id, { deadline } as Partial<Project>),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.projects.list() });
    },
  });

  if (!project) {
    return (
      <EmptyState
        title="Select a project"
        description="Choose a project from the rail or create a new one."
        icon={FolderKanban}
      />
    );
  }

  const displayStatus = PROJECT_STATUS_LABELS[project.status] ?? project.status;

  // Build the italic subtitle
  const subtitle = [
    project.description,
    project.deadline
      ? `due ${new Date(project.deadline).toLocaleString('en', { month: 'long' })}.`
      : null,
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <div className="flex h-full flex-col">
      {/* Breadcrumb + delete */}
      <div className="flex items-center justify-between border-b px-6 py-3">
        <nav aria-label="breadcrumb" className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Link to="/projects" className="hover:text-foreground hover:underline">Workspace</Link>
          <span>/</span>
          <Link to="/projects" className="hover:text-foreground hover:underline">Projects</Link>
          <span>/</span>
          <span className="font-medium text-foreground truncate max-w-[16rem]">{project.name}</span>
        </nav>
        <div className="flex items-center gap-2">
          {/* Status chip + inline select */}
          <Select
            value={project.status}
            onValueChange={(val) => statusMut.mutate(val)}
          >
            <SelectTrigger className="h-auto border-0 p-0 shadow-none focus:ring-0 bg-transparent" aria-label="Change status">
              <Badge
                variant={STATUS_VARIANTS[project.status] ?? 'secondary'}
                className="cursor-pointer text-xs"
              >
                {displayStatus}
              </Badge>
            </SelectTrigger>
            <SelectContent>
              {BACKEND_STATUSES.map((s) => (
                <SelectItem key={s} value={s}>
                  {PROJECT_STATUS_LABELS[s]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Button
            variant="ghost"
            size="icon"
            onClick={() => setShowDelete(true)}
            aria-label="Delete project"
          >
            <Trash2 className="h-4 w-4 text-muted-foreground" />
          </Button>
        </div>
      </div>

      {/* Chapter header */}
      <div className="border-b px-6 py-5">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <h2 className="font-serif text-2xl font-bold leading-snug tracking-tight text-strong">
              {project.name}
            </h2>
            {subtitle && (
              <p className="mt-1 text-sm italic text-muted-foreground leading-relaxed">
                {subtitle}
              </p>
            )}
          </div>
          {/* Deadline editor popover re-housed from OverviewTab */}
          <div className="flex shrink-0 items-center gap-2">
            <Popover>
              <PopoverTrigger asChild>
                <Button variant="ghost" size="sm" aria-label="Edit deadline" className="text-xs gap-1">
                  <Pencil className="h-3 w-3" />
                  {project.deadline
                    ? new Date(project.deadline).toLocaleDateString(undefined, { month: 'short', year: 'numeric' })
                    : 'No deadline'}
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-auto p-3 space-y-2">
                <input
                  type="date"
                  className="block w-full text-sm border rounded px-2 py-1"
                  value={project.deadline ?? ''}
                  onChange={(e) => deadlineMut.mutate(e.target.value || null)}
                />
                {project.deadline && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="w-full text-xs"
                    onClick={() => deadlineMut.mutate(null)}
                  >
                    Clear deadline
                  </Button>
                )}
              </PopoverContent>
            </Popover>
          </div>
        </div>
      </div>

      {/* Scrolling document pane */}
      <ScrollArea className="flex-1">
        <div className="space-y-10 px-6 py-6">
          {/* Open questions */}
          {questionsError && <ErrorSentinel message="Failed to load questions." />}
          <QuestionsSection projectId={project.id} questions={questions} />

          {/* Recent activity */}
          {activityError && <ErrorSentinel message="Failed to load activity." />}
          <RecentActivitySection items={activityItems} />

          {/* Milestones */}
          <section aria-labelledby="milestones-section-heading">
            <h3
              id="milestones-section-heading"
              className="mb-3 text-xs font-semibold tracking-widest text-muted-foreground uppercase"
            >
              MILESTONES · {milestones.length}
            </h3>
            {/* MilestonesTab renders its own load-failure state. */}
            <MilestonesTab projectId={project.id} />
          </section>

          {/* Tasks */}
          <section aria-labelledby="tasks-section-heading">
            <h3
              id="tasks-section-heading"
              className="mb-3 text-xs font-semibold tracking-widest text-muted-foreground uppercase"
            >
              TASKS · {tasks.length}
            </h3>
            {/* TasksTab renders its own load-failure state. */}
            <TasksTab projectId={project.id} />
          </section>

          {/* Papers */}
          <section aria-labelledby="papers-section-heading">
            <h3
              id="papers-section-heading"
              className="mb-3 text-xs font-semibold tracking-widest text-muted-foreground uppercase"
            >
              PAPERS · {project.paper_count ?? 0}
            </h3>
            <LinkedPapersTab projectId={project.id} />
          </section>
        </div>
      </ScrollArea>

      {/* Preserved CRUD dialogs from TasksTab/MilestonesTab/LinkedPapersTab are handled inside those components */}
      <ConfirmDialog
        open={showDelete}
        title="Delete project?"
        description={`This will permanently delete "${project.name}" and all its tasks and milestones.`}
        confirmLabel="Delete"
        onConfirm={() => deleteMut.mutate()}
        onCancel={() => setShowDelete(false)}
      />
    </div>
  );
}
