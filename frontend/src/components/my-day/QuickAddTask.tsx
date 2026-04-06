import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { createQuickTask, fetchProjects } from '@/lib/api';

export function QuickAddTask() {
  const [title, setTitle] = useState('');
  const [projectId, setProjectId] = useState<string>('none');
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const { data: projects } = useQuery({
    queryKey: ['projects', 'active'],
    queryFn: () => fetchProjects('active'),
  });

  const mutation = useMutation({
    mutationFn: createQuickTask,
    onSuccess: () => {
      setTitle('');
      setError(null);
      queryClient.invalidateQueries({ queryKey: ['my-day'] });
    },
    onError: () => setError('Failed to add task. Please try again.'),
  });

  const handleSubmit = () => {
    const trimmed = title.trim();
    if (!trimmed || mutation.isPending) return;
    mutation.mutate({
      title: trimmed,
      project_id: projectId === 'none' ? undefined : Number(projectId),
    });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') handleSubmit();
  };

  return (
    <div className="space-y-1">
      <div className="flex gap-2">
        <Input
          placeholder="Quick add task..."
          value={title}
          onChange={(e) => { setTitle(e.target.value); setError(null); }}
          onKeyDown={handleKeyDown}
          disabled={mutation.isPending}
          className="flex-1"
        />
        <Select value={projectId} onValueChange={(v) => { setProjectId(v); setError(null); }}>
          <SelectTrigger className="w-[140px]">
            <SelectValue placeholder="No project" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="none">No project</SelectItem>
            {projects?.map((p) => (
              <SelectItem key={p.id} value={String(p.id)}>
                {p.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}
