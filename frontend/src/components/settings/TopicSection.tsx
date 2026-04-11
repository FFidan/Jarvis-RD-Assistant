import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchTopics, createTopic, updateTopic, deleteTopic } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { EmptyState } from '@/components/EmptyState';
import { useConfirm } from '@/hooks/use-confirm';
import { Pencil, Trash2, Plus, Check, X, Tag } from 'lucide-react';
import type { Topic } from '@/types';

export function TopicSection() {
  const queryClient = useQueryClient();
  const { isOpen, confirm, handleConfirm, handleCancel } = useConfirm();
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState({ name: '', query_terms: '', category: '' });
  const [showAdd, setShowAdd] = useState(false);
  const [addForm, setAddForm] = useState({ name: '', query_terms: '', category: '' });
  const [deleteTarget, setDeleteTarget] = useState<number | null>(null);

  const { data: topics = [], isLoading } = useQuery({
    queryKey: ['topics'],
    queryFn: fetchTopics,
  });

  const createMut = useMutation({
    mutationFn: (data: Partial<Topic>) => createTopic(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['topics'] });
      setShowAdd(false);
      setAddForm({ name: '', query_terms: '', category: '' });
    },
  });

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<Topic> }) => updateTopic(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['topics'] });
      setEditingId(null);
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteTopic(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['topics'] });
    },
  });

  const startEdit = (topic: Topic) => {
    setEditingId(topic.id);
    setEditForm({
      name: topic.name,
      query_terms: topic.query_terms.join(', '),
      category: topic.category ?? '',
    });
  };

  const saveEdit = () => {
    if (!editingId) return;
    const terms = editForm.query_terms.split(',').map((t) => t.trim()).filter(Boolean);
    updateMut.mutate({
      id: editingId,
      data: {
        name: editForm.name,
        query_terms: terms,
        category: editForm.category || undefined,
      },
    });
  };

  const handleToggle = (topic: Topic) => {
    updateMut.mutate({ id: topic.id, data: { enabled: !topic.enabled } });
  };

  const handleDelete = async (id: number) => {
    setDeleteTarget(id);
    const confirmed = await confirm();
    if (confirmed) {
      deleteMut.mutate(id);
    }
    setDeleteTarget(null);
  };

  const handleAdd = () => {
    if (!addForm.name.trim()) return;
    const terms = addForm.query_terms.split(',').map((t) => t.trim()).filter(Boolean);
    createMut.mutate({
      name: addForm.name.trim(),
      query_terms: terms,
      category: addForm.category.trim() || undefined,
    } as Partial<Topic>);
  };

  if (isLoading) {
    return <div className="py-8 text-center text-muted-foreground">Loading topics...</div>;
  }

  return (
    <div className="space-y-4">
      {topics.length === 0 && !showAdd ? (
        <EmptyState title="No topics" description="Add a research topic to get started." icon={Tag} />
      ) : (
        <div className="space-y-2">
          {topics.map((topic) => (
            <Card key={topic.id}>
              <CardContent className="flex items-center gap-4 p-4">
                {editingId === topic.id ? (
                  <div className="flex flex-1 flex-wrap items-center gap-2">
                    <Input
                      value={editForm.name}
                      onChange={(e) => setEditForm({ ...editForm, name: e.target.value })}
                      placeholder="Name"
                      className="w-40"
                    />
                    <Input
                      value={editForm.query_terms}
                      onChange={(e) => setEditForm({ ...editForm, query_terms: e.target.value })}
                      placeholder="Query terms (comma-separated)"
                      className="w-60"
                    />
                    <Input
                      value={editForm.category}
                      onChange={(e) => setEditForm({ ...editForm, category: e.target.value })}
                      placeholder="Category"
                      className="w-32"
                    />
                    <Button size="icon" variant="ghost" onClick={saveEdit} disabled={updateMut.isPending}>
                      <Check className="h-4 w-4" />
                    </Button>
                    <Button size="icon" variant="ghost" onClick={() => setEditingId(null)}>
                      <X className="h-4 w-4" />
                    </Button>
                  </div>
                ) : (
                  <>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{topic.name}</span>
                        {topic.category && (
                          <Badge variant="secondary">{topic.category}</Badge>
                        )}
                        <Badge variant={topic.enabled ? 'default' : 'outline'}>
                          {topic.enabled ? 'Enabled' : 'Disabled'}
                        </Badge>
                      </div>
                      {topic.query_terms.length > 0 && (
                        <p className="mt-1 text-sm text-muted-foreground">
                          {topic.query_terms.join(', ')}
                        </p>
                      )}
                    </div>
                    <div className="flex items-center gap-1">
                      <Button size="sm" variant="ghost" onClick={() => handleToggle(topic)}>
                        {topic.enabled ? 'Disable' : 'Enable'}
                      </Button>
                      <Button size="icon" variant="ghost" onClick={() => startEdit(topic)}>
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button size="icon" variant="ghost" onClick={() => handleDelete(topic.id)}>
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {showAdd ? (
        <Card>
          <CardContent className="space-y-3 p-4">
            <div className="grid gap-2 sm:grid-cols-3">
              <div>
                <Label htmlFor="topic-name">Name</Label>
                <Input
                  id="topic-name"
                  value={addForm.name}
                  onChange={(e) => setAddForm({ ...addForm, name: e.target.value })}
                  placeholder="e.g. Transformers"
                />
              </div>
              <div>
                <Label htmlFor="topic-terms">Query Terms</Label>
                <Input
                  id="topic-terms"
                  value={addForm.query_terms}
                  onChange={(e) => setAddForm({ ...addForm, query_terms: e.target.value })}
                  placeholder="transformer, attention, BERT"
                />
              </div>
              <div>
                <Label htmlFor="topic-category">Category</Label>
                <Input
                  id="topic-category"
                  value={addForm.category}
                  onChange={(e) => setAddForm({ ...addForm, category: e.target.value })}
                  placeholder="NLP"
                />
              </div>
            </div>
            <div className="flex gap-2">
              <Button onClick={handleAdd} disabled={createMut.isPending}>
                Add Topic
              </Button>
              <Button variant="outline" onClick={() => setShowAdd(false)}>
                Cancel
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : (
        <Button variant="outline" onClick={() => setShowAdd(true)}>
          <Plus className="mr-2 h-4 w-4" />
          Add Topic
        </Button>
      )}

      <ConfirmDialog
        open={isOpen && deleteTarget !== null}
        title="Delete Topic"
        description="This will permanently remove this research topic. Are you sure?"
        confirmLabel="Delete"
        onConfirm={handleConfirm}
        onCancel={handleCancel}
      />
    </div>
  );
}
