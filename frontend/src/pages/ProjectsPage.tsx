import { useState, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useLocation } from 'react-router-dom';
import { FolderKanban } from 'lucide-react';
import { fetchProjects } from '@/lib/api';
import { Skeleton } from '@/components/ui/skeleton';
import { ProjectList } from '@/components/projects/ProjectList';
import { ProjectDetail } from '@/components/projects/ProjectDetail';

export function ProjectsPage() {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const location = useLocation();

  const { data: projects = [], isLoading } = useQuery({
    queryKey: ['projects'],
    queryFn: () => fetchProjects(),
  });

  useEffect(() => {
    const pid = (location.state as { projectId?: number } | null)?.projectId;
    if (pid != null && projects?.some((p: { id: number }) => p.id === pid)) {
      setSelectedId(pid);
    }
  }, [location.state, projects]);

  const selectedProject = selectedId
    ? projects.find((p) => p.id === selectedId) ?? null
    : null;

  if (isLoading) {
    return (
      <div className="space-y-4">
        <h1 className="text-3xl font-bold flex items-center gap-2">
          <FolderKanban className="h-8 w-8" /> Projects
        </h1>
        <p className="text-muted-foreground text-sm">Organize papers into research projects with tasks and milestones</p>
        <div className="grid grid-cols-3 gap-4">
          <div className="space-y-2">
            {[1, 2, 3].map((i) => <Skeleton key={i} className="h-20 w-full" />)}
          </div>
          <div className="col-span-2">
            <Skeleton className="h-96 w-full" />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-3xl font-bold flex items-center gap-2">
        <FolderKanban className="h-8 w-8" /> Projects
      </h1>
      <p className="text-muted-foreground text-sm">Organize papers into research projects with tasks and milestones</p>

      <div className="grid h-[calc(100vh-12rem)] grid-cols-1 gap-4 lg:grid-cols-3">
        {/* Left column: project list */}
        <div className="rounded-md border border-hair bg-card overflow-hidden shadow-none">
          <ProjectList
            projects={projects}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
        </div>

        {/* Right column: project detail */}
        <div className="rounded-md border border-hair bg-card overflow-hidden shadow-none lg:col-span-2">
          <ProjectDetail
            project={selectedProject}
            onDeleted={() => setSelectedId(null)}
          />
        </div>
      </div>
    </div>
  );
}
