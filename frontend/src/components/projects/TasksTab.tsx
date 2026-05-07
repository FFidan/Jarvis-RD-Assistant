import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2, Pencil, ListTodo } from 'lucide-react';
import type { Task } from '@/types';
import { fetchTasks, createTask, updateTask, deleteTask } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { EmptyState } from '@/components/EmptyState';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';

const TASK_STATUSES = ['todo', 'in_progress', 'blocked', 'done'] as const;

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  todo: 'outline',
  in_progress: 'default',
  blocked: 'destructive',
  done: 'secondary',
};

const PRIORITY_LABELS: Record<number, string> = {
  1: 'Critical',
  2: 'High',
  3: 'Medium',
  4: 'Low',
};

interface TasksTabProps {
  projectId: number;
}

export function TasksTab({ projectId }: TasksTabProps) {
  const queryClient = useQueryClient();
  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [title, setTitle] = useState('');
  const [priority, setPriority] = useState('3');
  const [deadline, setDeadline] = useState('');

  // Edit dialog state
  const [editTask, setEditTask] = useState<Task | null>(null);
  const [editTitle, setEditTitle] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [editPriority, setEditPriority] = useState('3');
  const [editDeadline, setEditDeadline] = useState('');
  const [editEstimatedHours, setEditEstimatedHours] = useState('');
  const [editActualHours, setEditActualHours] = useState('');

  const { data: tasks = [], isLoading } = useQuery({
    queryKey: ['tasks', projectId],
    queryFn: () => fetchTasks(projectId),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['tasks', projectId] });
    queryClient.invalidateQueries({ queryKey: ['projects'] });
  };

  const createMut = useMutation({
    mutationFn: () =>
      createTask(projectId, {
        title,
        priority: Number(priority),
        deadline: deadline || undefined,
      }),
    onSuccess: () => {
      invalidate();
      setShowCreate(false);
      setTitle('');
      setPriority('3');
      setDeadline('');
    },
  });

  const statusMut = useMutation({
    mutationFn: ({ id, status }: { id: number; status: string }) =>
      updateTask(id, { status } as Partial<Task>),
    onSuccess: invalidate,
  });

  const delMut = useMutation({
    mutationFn: (id: number) => deleteTask(id),
    onSuccess: () => {
      invalidate();
      setDeleteId(null);
    },
  });

  const openEdit = (task: Task) => {
    setEditTask(task);
    setEditTitle(task.title);
    setEditDescription(task.description ?? '');
    setEditPriority(String(task.priority));
    setEditDeadline(task.deadline ?? '');
    setEditEstimatedHours(task.estimated_hours != null ? String(task.estimated_hours) : '');
    setEditActualHours(task.actual_hours != null ? String(task.actual_hours) : '');
  };

  const editMut = useMutation({
    mutationFn: () => {
      if (!editTask) throw new Error('No task selected');
      return updateTask(editTask.id, {
        title: editTitle,
        description: editDescription || null,
        priority: Number(editPriority),
        deadline: editDeadline || null,
        estimated_hours: editEstimatedHours ? Number(editEstimatedHours) : null,
        actual_hours: editActualHours ? Number(editActualHours) : null,
      } as Partial<Task>);
    },
    onSuccess: () => {
      invalidate();
      setEditTask(null);
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
        <span className="text-xs text-muted-foreground">{`${tasks.length} task${tasks.length !== 1 ? 's' : ''}`}</span>
        <Button size="sm" onClick={() => setShowCreate(true)}>
          <Plus className="mr-1 h-4 w-4" /> Add Task
        </Button>
      </div>

      {tasks.length === 0 ? (
        <EmptyState
          title="No tasks"
          description="Add tasks to break down your project work."
          icon={ListTodo}
        />
      ) : (
        <div className="space-y-2">
          {tasks.map((task) => (
            <div
              key={task.id}
              className="flex items-center gap-3 rounded-md border p-3"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className={`text-sm font-medium ${task.status === 'done' ? 'line-through text-muted-foreground' : ''}`}>
                    {task.title}
                  </span>
                  <Badge variant="outline" className="text-xs shrink-0">
                    {PRIORITY_LABELS[task.priority] ?? `P${task.priority}`}
                  </Badge>
                </div>
                {task.deadline && (
                  <span className="text-xs text-muted-foreground">Due: {task.deadline}</span>
                )}
              </div>
              <Select
                value={task.status}
                onValueChange={(val) => statusMut.mutate({ id: task.id, status: val })}
              >
                <SelectTrigger className="w-[130px] shrink-0">
                  <Badge variant={STATUS_VARIANT[task.status] ?? 'outline'} className="text-xs">
                    {task.status.replace('_', ' ')}
                  </Badge>
                </SelectTrigger>
                <SelectContent>
                  {TASK_STATUSES.map((s) => (
                    <SelectItem key={s} value={s}>
                      {s.replace('_', ' ')}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => openEdit(task)}
                className="shrink-0"
              >
                <Pencil className="h-4 w-4 text-muted-foreground" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setDeleteId(task.id)}
                className="shrink-0"
              >
                <Trash2 className="h-4 w-4 text-muted-foreground" />
              </Button>
            </div>
          ))}
        </div>
      )}

      <Dialog open={showCreate} onOpenChange={setShowCreate}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add Task</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label htmlFor="task-title">Title</Label>
              <Input id="task-title" value={title} onChange={(e) => setTitle(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="task-priority">Priority</Label>
              <Select value={priority} onValueChange={setPriority}>
                <SelectTrigger id="task-priority">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {[1, 2, 3, 4].map((p) => (
                    <SelectItem key={p} value={String(p)}>
                      {PRIORITY_LABELS[p]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="task-deadline">Deadline</Label>
              <Input id="task-deadline" type="date" value={deadline} onChange={(e) => setDeadline(e.target.value)} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowCreate(false)}>Cancel</Button>
            <Button onClick={() => createMut.mutate()} disabled={!title.trim() || createMut.isPending}>
              {createMut.isPending ? 'Adding...' : 'Add'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={editTask !== null} onOpenChange={(open) => { if (!open) setEditTask(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Task</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label htmlFor="edit-task-title">Title</Label>
              <Input id="edit-task-title" value={editTitle} onChange={(e) => setEditTitle(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-task-desc">Description</Label>
              <Textarea id="edit-task-desc" value={editDescription} onChange={(e) => setEditDescription(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-task-priority">Priority</Label>
              <Select value={editPriority} onValueChange={setEditPriority}>
                <SelectTrigger id="edit-task-priority">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {[1, 2, 3, 4].map((p) => (
                    <SelectItem key={p} value={String(p)}>
                      {PRIORITY_LABELS[p]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-task-deadline">Deadline</Label>
              <Input id="edit-task-deadline" type="date" value={editDeadline} onChange={(e) => setEditDeadline(e.target.value)} />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="edit-task-est">Estimated Hours</Label>
                <Input id="edit-task-est" type="number" min="0" step="0.5" value={editEstimatedHours} onChange={(e) => setEditEstimatedHours(e.target.value)} />
              </div>
              <div className="space-y-2">
                <Label htmlFor="edit-task-actual">Actual Hours</Label>
                <Input id="edit-task-actual" type="number" min="0" step="0.5" value={editActualHours} onChange={(e) => setEditActualHours(e.target.value)} />
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditTask(null)}>Cancel</Button>
            <Button onClick={() => editMut.mutate()} disabled={!editTitle.trim() || editMut.isPending}>
              {editMut.isPending ? 'Saving...' : 'Save'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={deleteId !== null}
        title="Delete task?"
        description="This action cannot be undone."
        confirmLabel="Delete"
        onConfirm={() => deleteId && delMut.mutate(deleteId)}
        onCancel={() => setDeleteId(null)}
      />
    </div>
  );
}
