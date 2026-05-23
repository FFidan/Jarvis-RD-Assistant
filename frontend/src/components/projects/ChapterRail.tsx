import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Plus, FolderKanban } from 'lucide-react';
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

// §3.2 — map backend status values to display chip labels
const STATUS_DISPLAY: Record<string, string> = {
  active: 'reading',
  paused: 'drafting',
  completed: 'shipped',
  archived: 'idle',
};

const STATUS_VARIANTS: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  active: 'default',
  paused: 'secondary',
  completed: 'outline',
  archived: 'secondary',
};

/** Convert a 1-based integer to a Roman numeral string. */
export function toRoman(n: number): string {
  const vals = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1] as const;
  const syms = ['M', 'CM', 'D', 'CD', 'C', 'XC', 'L', 'XL', 'X', 'IX', 'V', 'IV', 'I'] as const;
  let result = '';
  let remaining = n;
  for (let i = 0; i < vals.length; i++) {
    const v = vals[i]!;
    const s = syms[i]!;
    while (remaining >= v) {
      result += s;
      remaining -= v;
    }
  }
  return result;
}

interface ChapterRailProps {
  projects: Project[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}

export function ChapterRail({ projects, selectedId, onSelect }: ChapterRailProps) {
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const queryClient = useQueryClient();

  const createMut = useMutation({
    mutationFn: (data: { name: string; description?: string }) => createProject(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.projects.list() });
      setShowCreate(false);
      setNewName('');
      setNewDesc('');
    },
  });

  return (
    <div className="flex h-full flex-col">
      {/* Rail header */}
      <div className="px-4 pt-4 pb-2">
        <span className="text-xs font-semibold tracking-widest text-muted-foreground uppercase">
          § CHAPTERS · {projects.length}
        </span>
      </div>

      {/* Chapter list */}
      <ScrollArea className="flex-1 px-2">
        {projects.length === 0 ? (
          <div className="px-2">
            <EmptyState
              title="No chapters yet"
              description="Create a project to start a research chapter."
              icon={FolderKanban}
            />
          </div>
        ) : (
          <div className="space-y-1 p-2">
            {projects.map((project, idx) => (
              <button
                key={project.id}
                onClick={() => onSelect(project.id)}
                aria-pressed={selectedId === project.id}
                className={cn(
                  'flex w-full flex-col items-start gap-1 rounded-md border-l-2 border-transparent px-3 py-2.5 text-left text-sm transition-colors hover:bg-accent',
                  selectedId === project.id &&
                    'border-l-[hsl(var(--ring))] bg-accent text-strong',
                )}
              >
                {/* Row 1: roman numeral + name + status chip */}
                <div className="flex w-full items-center gap-2">
                  <span className="shrink-0 text-xs font-mono text-muted-foreground w-6 text-right">
                    {toRoman(idx + 1)}
                  </span>
                  <span className="flex-1 font-medium truncate">{project.name}</span>
                  <Badge
                    variant={STATUS_VARIANTS[project.status] ?? 'secondary'}
                    className="ml-1 shrink-0 text-[10px] px-1.5 py-0"
                  >
                    {STATUS_DISPLAY[project.status] ?? project.status}
                  </Badge>
                </div>
                {/* Row 2: counts */}
                <div className="flex items-center gap-3 pl-8 text-xs text-muted-foreground">
                  <span>{project.paper_count ?? 0} papers</span>
                  <span>{project.open_question_count ?? 0} Qs</span>
                </div>
              </button>
            ))}
          </div>
        )}
      </ScrollArea>

      {/* Rail footer: create button */}
      <div className="border-t p-3">
        <Button
          size="sm"
          variant="outline"
          className="w-full"
          onClick={() => setShowCreate(true)}
          aria-label="Create project"
        >
          <Plus className="mr-1 h-4 w-4" />
          New Chapter
        </Button>
      </div>

      {/* Create dialog */}
      <Dialog open={showCreate} onOpenChange={setShowCreate}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create Project</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label htmlFor="chapter-name">Name</Label>
              <Input
                id="chapter-name"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="Project name"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="chapter-desc">Description</Label>
              <Textarea
                id="chapter-desc"
                value={newDesc}
                onChange={(e) => setNewDesc(e.target.value)}
                placeholder="Optional description"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowCreate(false)}>
              Cancel
            </Button>
            <Button
              onClick={() =>
                createMut.mutate({ name: newName, description: newDesc || undefined })
              }
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
