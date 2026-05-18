import { useState, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useLocation } from 'react-router-dom';
import { FolderKanban } from 'lucide-react';
import { fetchProjects } from '@/lib/api';
import { Skeleton } from '@/components/ui/skeleton';
import { QueryErrorState } from '@/components/shared/QueryErrorState';
import { ChapterRail } from '@/components/projects/ChapterRail';
import { ChapterPane } from '@/components/projects/ChapterPane';

export function ProjectsPage() {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const location = useLocation();

  const { data: projects = [], isLoading, isError } = useQuery({
    queryKey: ['projects'],
    queryFn: () => fetchProjects(),
  });

  // §3.7 — deep-link from My-Day ProjectPulse or auto-select first chapter
  useEffect(() => {
    const pid = (location.state as { projectId?: number } | null)?.projectId;
    if (pid != null && projects?.some((p: { id: number }) => p.id === pid)) {
      setSelectedId(pid);
    } else if (selectedId === null && projects.length > 0) {
      // Auto-select first chapter when no deep-link present
      const first = projects[0];
      if (first) setSelectedId(first.id);
    }
  }, [location.state, projects]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectedProject = selectedId
    ? projects.find((p) => p.id === selectedId) ?? null
    : null;

  if (isError) {
    return <QueryErrorState />;
  }

  if (isLoading) {
    return (
      <div className="space-y-4">
        <h1 className="text-[32px] leading-tight tracking-tight text-strong flex items-center gap-2">
          <FolderKanban className="h-8 w-8" /> Projects
        </h1>
        <p className="text-muted-foreground text-sm">
          Organize papers into research projects with tasks and milestones
        </p>
        <div className="grid grid-cols-4 gap-4">
          <div className="space-y-2">
            {[1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-16 w-full" />
            ))}
          </div>
          <div className="col-span-3">
            <Skeleton className="h-96 w-full" />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-[32px] leading-tight tracking-tight text-strong flex items-center gap-2">
        <FolderKanban className="h-8 w-8" /> Projects
      </h1>
      <p className="text-muted-foreground text-sm">
        Organize papers into research projects with tasks and milestones
      </p>

      {/* 2-pane layout: fixed ~240px chapter rail + wider document pane */}
      <div className="flex h-[calc(100dvh-12rem)] gap-4 overflow-hidden">
        {/* Left rail */}
        <div className="w-60 shrink-0 rounded-md border border-hair bg-card overflow-hidden shadow-none">
          <ChapterRail
            projects={projects}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
        </div>

        {/* Main document pane */}
        <div className="flex-1 rounded-md border border-hair bg-card overflow-hidden shadow-none">
          <ChapterPane
            project={selectedProject}
            onDeleted={() => setSelectedId(projects.find((p) => p.id !== selectedId)?.id ?? null)}
          />
        </div>
      </div>
    </div>
  );
}
