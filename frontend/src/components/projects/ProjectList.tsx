import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, FolderKanban, Search } from 'lucide-react';
import type { Project } from '@/types';
import { createProject } from '@/lib/api';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { EmptyState } from '@/components/EmptyState';
import { SectionHeader } from '@/components/my-day/sections/SectionHeader';

const STATUS_VARIANTS: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  active: 'default',
  paused: 'secondary',
  completed: 'outline',
  archived: 'secondary',
};

interface ProjectListProps {
  projects: Project[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}

export function ProjectList({ projects, selectedId, onSelect }: ProjectListProps) {
  const [search, setSearch] = useState('');
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const queryClient = useQueryClient();

  const createMut = useMutation({
    mutationFn: (data: { name: string; description?: string }) => createProject(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      setShowCreate(false);
      setNewName('');
      setNewDesc('');
    },
  });

  const filtered = projects.filter((p) =>
    p.name.toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 p-4 pb-2">
        <div className="relative flex-1">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search projects..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-8"
          />
        </div>
        <Button size="icon" onClick={() => setShowCreate(true)} title="Create project">
          <Plus className="h-4 w-4" />
        </Button>
      </div>

      <div className="px-4 pt-2">
        <SectionHeader marker="PROJECTS" meta={`${projects.length} project${projects.length !== 1 ? 's' : ''}`} />
      </div>
      <ScrollArea className="flex-1 px-2">
        {filtered.length === 0 ? (
          <EmptyState
            title={projects.length === 0 ? 'No projects yet' : 'No matches'}
            description={projects.length === 0 ? 'Create a project to organize papers and track research goals.' : 'Try a different search term.'}
            icon={FolderKanban}
          />
        ) : (
          <div className="space-y-1 p-2">
            {filtered.map((project) => (
              <button
                key={project.id}
                onClick={() => onSelect(project.id)}
                className={cn(
                  'flex w-full flex-col items-start gap-1 rounded-md border p-3 text-left text-sm transition-colors hover:bg-accent',
                  selectedId === project.id && 'bg-accent border-primary',
                )}
              >
                <div className="flex w-full items-center justify-between">
                  <span className="font-medium truncate">{project.name}</span>
                  <Badge variant={STATUS_VARIANTS[project.status] ?? 'secondary'} className="ml-2 shrink-0">
                    {project.status}
                  </Badge>
                </div>
                {project.deadline && (
                  <span className="text-xs text-muted-foreground">
                    Due: {project.deadline}
                  </span>
                )}
                <span className="text-xs text-muted-foreground">
                  Created: {new Date(project.created_at).toLocaleDateString()}
                </span>
              </button>
            ))}
          </div>
        )}
      </ScrollArea>

      <Dialog open={showCreate} onOpenChange={setShowCreate}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create Project</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label htmlFor="project-name">Name</Label>
              <Input
                id="project-name"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="Project name"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="project-desc">Description</Label>
              <Textarea
                id="project-desc"
                value={newDesc}
                onChange={(e) => setNewDesc(e.target.value)}
                placeholder="Optional description"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowCreate(false)}>Cancel</Button>
            <Button
              onClick={() => createMut.mutate({ name: newName, description: newDesc || undefined })}
              disabled={!newName.trim() || createMut.isPending}
            >
              {createMut.isPending ? 'Creating...' : 'Create'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
