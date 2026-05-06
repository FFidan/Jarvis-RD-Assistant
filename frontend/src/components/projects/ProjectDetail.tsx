import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Trash2, FolderKanban } from 'lucide-react';
import type { Project } from '@/types';
import { deleteProject } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { EmptyState } from '@/components/EmptyState';
import { OverviewTab } from './OverviewTab';
import { MilestonesTab } from './MilestonesTab';
import { TasksTab } from './TasksTab';
import { LinkedPapersTab } from './LinkedPapersTab';

const STATUS_VARIANTS: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  active: 'default',
  paused: 'secondary',
  completed: 'outline',
  archived: 'secondary',
};

interface ProjectDetailProps {
  project: Project | null;
  onDeleted: () => void;
}

export function ProjectDetail({ project, onDeleted }: ProjectDetailProps) {
  const queryClient = useQueryClient();
  const [showDelete, setShowDelete] = useState(false);

  const deleteMut = useMutation({
    mutationFn: () => deleteProject(project!.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      setShowDelete(false);
      onDeleted();
    },
  });

  if (!project) {
    return (
      <EmptyState
        title="Select a project"
        description="Choose a project from the list or create a new one."
        icon={FolderKanban}
      />
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b p-4">
        <div className="flex items-center gap-3 min-w-0">
          <h2 className="text-xl font-bold truncate">{project.name}</h2>
          <Badge variant={STATUS_VARIANTS[project.status] ?? 'secondary'}>
            {project.status}
          </Badge>
        </div>
        <Button variant="ghost" size="icon" onClick={() => setShowDelete(true)} aria-label="Delete project">
          <Trash2 className="h-4 w-4 text-muted-foreground" />
        </Button>
      </div>

      <div className="flex-1 overflow-auto p-4">
        <Tabs defaultValue="overview">
          <TabsList className="bg-transparent border-b border-hair p-0 gap-2">
            <TabsTrigger value="overview" className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none">Overview</TabsTrigger>
            <TabsTrigger value="milestones" className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none">Milestones</TabsTrigger>
            <TabsTrigger value="tasks" className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none">Tasks</TabsTrigger>
            <TabsTrigger value="papers" className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none">Papers</TabsTrigger>
          </TabsList>
          <TabsContent value="overview" className="mt-4">
            <OverviewTab project={project} />
          </TabsContent>
          <TabsContent value="milestones" className="mt-4">
            <MilestonesTab projectId={project.id} />
          </TabsContent>
          <TabsContent value="tasks" className="mt-4">
            <TasksTab projectId={project.id} />
          </TabsContent>
          <TabsContent value="papers" className="mt-4">
            <LinkedPapersTab projectId={project.id} />
          </TabsContent>
        </Tabs>
      </div>

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
