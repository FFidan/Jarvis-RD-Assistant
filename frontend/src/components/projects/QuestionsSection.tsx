import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Plus, Trash2, HelpCircle } from 'lucide-react';
import { createProjectQuestion, deleteProjectQuestion } from '@/lib/api';
import type { ProjectQuestion } from '@/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { EmptyState } from '@/components/EmptyState';

interface QuestionsSectionProps {
  projectId: number;
  questions: ProjectQuestion[];
}

export function QuestionsSection({ projectId, questions }: QuestionsSectionProps) {
  const queryClient = useQueryClient();
  const [newBody, setNewBody] = useState('');
  const [deleteId, setDeleteId] = useState<number | null>(null);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: QUERY_KEYS.projects.questions(projectId) });

  const invalidateProjects = () =>
    queryClient.invalidateQueries({ queryKey: QUERY_KEYS.projects.list() });

  const createMut = useMutation({
    mutationFn: (body: string) => createProjectQuestion(projectId, body),
    onSuccess: () => {
      invalidate();
      invalidateProjects();
      setNewBody('');
    },
  });

  const deleteMut = useMutation({
    mutationFn: (questionId: number) => deleteProjectQuestion(questionId),
    onSuccess: () => {
      invalidate();
      invalidateProjects();
      setDeleteId(null);
    },
  });

  const handleAdd = () => {
    if (!newBody.trim()) return;
    createMut.mutate(newBody.trim());
  };

  return (
    <section aria-labelledby="open-questions-heading">
      <h3
        id="open-questions-heading"
        className="mb-3 text-xs font-semibold tracking-widest text-muted-foreground uppercase"
      >
        § OPEN QUESTIONS · {questions.length}
      </h3>

      {questions.length === 0 ? (
        <EmptyState
          title="No open questions"
          description="Add questions to track unresolved research problems."
          icon={HelpCircle}
        />
      ) : (
        <ol className="mb-4 space-y-2">
          {questions.map((q, idx) => (
            <li key={q.id} className="flex items-start gap-3 rounded-md border p-3">
              <span className="shrink-0 text-xs font-mono text-muted-foreground pt-0.5 w-6">
                Q{idx + 1}
              </span>
              <p className="flex-1 text-sm leading-relaxed">{q.body}</p>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Delete question"
                onClick={() => setDeleteId(q.id)}
                className="shrink-0 h-7 w-7"
              >
                <Trash2 className="h-3.5 w-3.5 text-muted-foreground" />
              </Button>
            </li>
          ))}
        </ol>
      )}

      {/* Inline add */}
      <div className="flex gap-2">
        <Input
          placeholder="Add an open question…"
          value={newBody}
          onChange={(e) => setNewBody(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
          className="flex-1 text-sm"
          aria-label="New question body"
        />
        <Button
          size="sm"
          onClick={handleAdd}
          disabled={!newBody.trim() || createMut.isPending}
          aria-label="Add question"
        >
          <Plus className="h-4 w-4" />
        </Button>
      </div>

      <ConfirmDialog
        open={deleteId !== null}
        title="Delete question?"
        description="This action cannot be undone."
        confirmLabel="Delete"
        onConfirm={() => deleteId !== null && deleteMut.mutate(deleteId)}
        onCancel={() => setDeleteId(null)}
      />
    </section>
  );
}
