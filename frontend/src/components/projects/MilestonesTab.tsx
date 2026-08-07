import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Plus, Trash2, Pencil, Flag } from 'lucide-react';
import type { Milestone } from '@/types';
import { fetchMilestones, createMilestone, updateMilestone, deleteMilestone } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { EmptyState } from '@/components/EmptyState';
import { QueryErrorState } from '@/components/shared/QueryErrorState';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';

interface MilestonesTabProps {
  projectId: number;
}

export function MilestonesTab({ projectId }: MilestonesTabProps) {
  const queryClient = useQueryClient();
  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [deadline, setDeadline] = useState('');

  // Edit dialog state
  const [editMs, setEditMs] = useState<Milestone | null>(null);
  const [editName, setEditName] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [editDeadline, setEditDeadline] = useState('');

  const { data: milestones = [], isLoading, isError } = useQuery({
    queryKey: QUERY_KEYS.projects.milestones(projectId),
    queryFn: () => fetchMilestones(projectId),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: QUERY_KEYS.projects.milestones(projectId) });
  };

  const createMut = useMutation({
    mutationFn: () =>
      createMilestone(projectId, {
        name,
        description: description || null,
        deadline: deadline || null,
      }),
    onSuccess: () => {
      invalidate();
      setShowCreate(false);
      setName('');
      setDescription('');
      setDeadline('');
    },
  });

  const toggleMut = useMutation({
    mutationFn: ({ id, completed }: { id: number; completed: boolean }) =>
      updateMilestone(id, { completed } as Parameters<typeof updateMilestone>[1]),
    onSuccess: invalidate,
  });

  const delMut = useMutation({
    mutationFn: (id: number) => deleteMilestone(id),
    onSuccess: () => {
      invalidate();
      setDeleteId(null);
    },
  });

  const openEditMs = (ms: Milestone) => {
    setEditMs(ms);
    setEditName(ms.name);
    setEditDescription(ms.description ?? '');
    setEditDeadline(ms.deadline ?? '');
  };

  const editMut = useMutation({
    mutationFn: () => {
      if (!editMs) throw new Error('No milestone selected');
      return updateMilestone(editMs.id, {
        name: editName,
        description: editDescription || null,
        deadline: editDeadline || null,
      } as Partial<Milestone>);
    },
    onSuccess: () => {
      invalidate();
      setEditMs(null);
    },
  });

  if (isLoading) {
    return (
      <div className="space-y-3">
        {[1, 2, 3].map((i) => <Skeleton key={i} className="h-14 w-full" />)}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        {isError ? (
          <span aria-hidden="true" />
        ) : (
          <span className="text-xs text-muted-foreground">{`${milestones.length} milestone${milestones.length !== 1 ? 's' : ''}`}</span>
        )}
        <Button size="sm" onClick={() => setShowCreate(true)}>
          <Plus className="mr-1 h-4 w-4" /> Add Milestone
        </Button>
      </div>

      {isError && <QueryErrorState message="Failed to load milestones." />}
      {!isError && (milestones.length === 0 ? (
        <EmptyState
          title="No milestones"
          description="Add milestones to track key deliverables."
          icon={Flag}
        />
      ) : (
        <div className="space-y-2">
          {milestones.map((ms) => (
            <div
              key={ms.id}
              className="flex items-center gap-3 rounded-md border p-3"
            >
              <input
                type="checkbox"
                checked={ms.completed}
                onChange={() => toggleMut.mutate({ id: ms.id, completed: !ms.completed })}
                className="h-4 w-4 rounded border-gray-300"
              />
              <div className="flex-1 min-w-0">
                <p className={`text-sm font-medium ${ms.completed ? 'line-through text-muted-foreground' : ''}`}>
                  {ms.name}
                </p>
                {ms.description && (
                  <p className="text-xs text-muted-foreground truncate">{ms.description}</p>
                )}
              </div>
              {ms.deadline && (
                <span className="text-xs text-muted-foreground shrink-0">
                  {ms.deadline}
                </span>
              )}
              <Button
                variant="ghost"
                size="icon"
                onClick={() => openEditMs(ms)}
                className="shrink-0"
              >
                <Pencil className="h-4 w-4 text-muted-foreground" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setDeleteId(ms.id)}
                className="shrink-0"
              >
                <Trash2 className="h-4 w-4 text-muted-foreground" />
              </Button>
            </div>
          ))}
        </div>
      ))}

      <Dialog open={showCreate} onOpenChange={setShowCreate}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add Milestone</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label htmlFor="ms-name">Name</Label>
              <Input id="ms-name" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="ms-desc">Description</Label>
              <Textarea id="ms-desc" value={description} onChange={(e) => setDescription(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="ms-deadline">Deadline</Label>
              <Input id="ms-deadline" type="date" value={deadline} onChange={(e) => setDeadline(e.target.value)} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowCreate(false)}>Cancel</Button>
            <Button onClick={() => createMut.mutate()} disabled={!name.trim() || !deadline.trim() || createMut.isPending}>
              {createMut.isPending ? 'Adding...' : 'Add'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={editMs !== null} onOpenChange={(open) => { if (!open) setEditMs(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Milestone</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label htmlFor="edit-ms-name">Name</Label>
              <Input id="edit-ms-name" value={editName} onChange={(e) => setEditName(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-ms-desc">Description</Label>
              <Textarea id="edit-ms-desc" value={editDescription} onChange={(e) => setEditDescription(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-ms-deadline">Deadline</Label>
              <Input id="edit-ms-deadline" type="date" value={editDeadline} onChange={(e) => setEditDeadline(e.target.value)} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditMs(null)}>Cancel</Button>
            <Button onClick={() => editMut.mutate()} disabled={!editName.trim() || !editDeadline.trim() || editMut.isPending}>
              {editMut.isPending ? 'Saving...' : 'Save'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={deleteId !== null}
        title="Delete milestone?"
        description="This action cannot be undone."
        confirmLabel="Delete"
        onConfirm={() => deleteId && delMut.mutate(deleteId)}
        onCancel={() => setDeleteId(null)}
      />
    </div>
  );
}
