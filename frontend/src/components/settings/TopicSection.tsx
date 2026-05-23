import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import {
  fetchTopics,
  createTopic,
  updateTopic,
  deleteTopic,
  fetchMySubscriptions,
  subscribeToTopic,
  unsubscribeFromTopic,
} from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { EmptyState } from '@/components/EmptyState';
import { useConfirm } from '@/hooks/use-confirm';
import { Pencil, Trash2, Plus, Check, X, Tag } from 'lucide-react';
import type { Topic } from '@/types';

const DESCRIPTION_TOOLTIP =
  'Free-text context that the Pulse scoring LLM uses when ranking candidate papers.';

const TOPIC_FIELD_TOOLTIPS = {
  name: "A short name for this research area, e.g. 'Transformers' or 'Climate ML'.",
  queryTerms: "Comma-separated keywords used to search paper databases. Broader terms surface more papers; narrower terms are more precise.",
  category: "Optional grouping label shown in the Topics list. Useful if you have many topics across different fields.",
} as const;

export function TopicSection() {
  const queryClient = useQueryClient();
  const { isOpen, confirm, handleConfirm, handleCancel } = useConfirm();
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState({ name: '', query_terms: '', category: '', description: '' });
  const [showAdd, setShowAdd] = useState(false);
  const [addForm, setAddForm] = useState({ name: '', query_terms: '', category: '', description: '' });
  const [deleteTarget, setDeleteTarget] = useState<number | null>(null);

  const { data: topics = [], isLoading } = useQuery({
    queryKey: QUERY_KEYS.topics.list(),
    queryFn: fetchTopics,
  });

  const { data: subscriptions = [] } = useQuery({
    queryKey: QUERY_KEYS.topics.subscriptions(),
    queryFn: fetchMySubscriptions,
  });

  const subscribeMut = useMutation({
    mutationFn: (topicId: number) => subscribeToTopic(topicId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.topics.subscriptions() }),
  });

  const unsubscribeMut = useMutation({
    mutationFn: (topicId: number) => unsubscribeFromTopic(topicId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.topics.subscriptions() }),
  });

  const handleSubscriptionToggle = (topic: Topic, checked: boolean) => {
    if (checked) {
      subscribeMut.mutate(topic.id);
    } else {
      unsubscribeMut.mutate(topic.id);
    }
  };

  const createMut = useMutation({
    mutationFn: (data: Partial<Topic>) => createTopic(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.topics.list() });
      setShowAdd(false);
      setAddForm({ name: '', query_terms: '', category: '', description: '' });
    },
  });

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<Topic> }) => updateTopic(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.topics.list() });
      setEditingId(null);
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteTopic(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.topics.list() });
    },
  });

  const startEdit = (topic: Topic) => {
    setEditingId(topic.id);
    setEditForm({
      name: topic.name,
      query_terms: topic.query_terms.join(', '),
      category: topic.category ?? '',
      description: topic.description ?? '',
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
        description: editForm.description.trim() || null,
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
      description: addForm.description.trim() || null,
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
            <Card key={topic.id} className="rounded-md border-hair shadow-none">
              <CardContent className="flex items-center gap-4 p-4">
                {editingId === topic.id ? (
                  <div className="flex flex-1 flex-col gap-2">
                    <div className="flex flex-wrap items-end gap-2">
                      <div className="flex flex-col gap-1">
                        <label className="text-xs text-muted-foreground flex items-center gap-1">
                          Name
                          <InfoTooltip content={TOPIC_FIELD_TOOLTIPS.name} />
                        </label>
                        <Input
                          value={editForm.name}
                          onChange={(e) => setEditForm({ ...editForm, name: e.target.value })}
                          placeholder="Name"
                          className="w-40"
                        />
                      </div>
                      <div className="flex flex-col gap-1">
                        <label className="text-xs text-muted-foreground flex items-center gap-1">
                          Query Terms
                          <InfoTooltip content={TOPIC_FIELD_TOOLTIPS.queryTerms} />
                        </label>
                        <Input
                          value={editForm.query_terms}
                          onChange={(e) => setEditForm({ ...editForm, query_terms: e.target.value })}
                          placeholder="Query terms (comma-separated)"
                          className="w-60"
                        />
                      </div>
                      <div className="flex flex-col gap-1">
                        <label className="text-xs text-muted-foreground flex items-center gap-1">
                          Category
                          <InfoTooltip content={TOPIC_FIELD_TOOLTIPS.category} />
                        </label>
                        <Input
                          value={editForm.category}
                          onChange={(e) => setEditForm({ ...editForm, category: e.target.value })}
                          placeholder="Category"
                          className="w-32"
                        />
                      </div>
                      <Button size="icon" variant="ghost" onClick={saveEdit} disabled={updateMut.isPending} aria-label="Save topic">
                        <Check className="h-4 w-4" />
                      </Button>
                      <Button size="icon" variant="ghost" onClick={() => setEditingId(null)} aria-label="Cancel edit">
                        <X className="h-4 w-4" />
                      </Button>
                    </div>
                    <div className="flex items-start gap-2">
                      <Label
                        htmlFor={`topic-edit-description-${topic.id}`}
                        className="mt-2 flex items-center gap-1 text-xs"
                      >
                        Description
                        <InfoTooltip content={DESCRIPTION_TOOLTIP} />
                      </Label>
                      <Textarea
                        id={`topic-edit-description-${topic.id}`}
                        value={editForm.description}
                        onChange={(e) => setEditForm({ ...editForm, description: e.target.value })}
                        placeholder="Optional context for the Pulse scoring LLM"
                        rows={2}
                        maxLength={1000}
                        className="flex-1 text-sm"
                      />
                    </div>
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
                    <div className="flex items-center gap-3">
                      <div className="flex items-center gap-2">
                        <Switch
                          id={`sub-${topic.id}`}
                          checked={subscriptions.includes(topic.id)}
                          onCheckedChange={(checked) => handleSubscriptionToggle(topic, checked)}
                          aria-label="Auto-add matches to my library"
                        />
                        <label
                          htmlFor={`sub-${topic.id}`}
                          className="cursor-pointer text-xs text-muted-foreground"
                          title="When enabled, papers matching this topic are automatically added to your library during auto-fetch."
                        >
                          Auto-add matches
                        </label>
                      </div>
                      <div className="flex items-center gap-1">
                        <Button size="sm" variant="ghost" onClick={() => handleToggle(topic)}>
                          {topic.enabled ? 'Disable' : 'Enable'}
                        </Button>
                        <Button size="icon" variant="ghost" onClick={() => startEdit(topic)} aria-label="Edit topic">
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button size="icon" variant="ghost" onClick={() => handleDelete(topic.id)} aria-label="Delete topic">
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </div>
                  </>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {showAdd ? (
        <Card className="rounded-md border-hair shadow-none">
          <CardContent className="space-y-3 p-4">
            <div className="grid gap-2 sm:grid-cols-3">
              <div>
                <Label htmlFor="topic-name" className="flex items-center gap-1">
                  Name
                  <InfoTooltip content={TOPIC_FIELD_TOOLTIPS.name} />
                </Label>
                <Input
                  id="topic-name"
                  value={addForm.name}
                  onChange={(e) => setAddForm({ ...addForm, name: e.target.value })}
                  placeholder="e.g. Transformers"
                />
              </div>
              <div>
                <Label htmlFor="topic-terms" className="flex items-center gap-1">
                  Query Terms
                  <InfoTooltip content={TOPIC_FIELD_TOOLTIPS.queryTerms} />
                </Label>
                <Input
                  id="topic-terms"
                  value={addForm.query_terms}
                  onChange={(e) => setAddForm({ ...addForm, query_terms: e.target.value })}
                  placeholder="transformer, attention, BERT"
                />
              </div>
              <div>
                <Label htmlFor="topic-category" className="flex items-center gap-1">
                  Category
                  <InfoTooltip content={TOPIC_FIELD_TOOLTIPS.category} />
                </Label>
                <Input
                  id="topic-category"
                  value={addForm.category}
                  onChange={(e) => setAddForm({ ...addForm, category: e.target.value })}
                  placeholder="NLP"
                />
              </div>
            </div>
            <div>
              <Label htmlFor="topic-description" className="flex items-center gap-1">
                Description
                <InfoTooltip content={DESCRIPTION_TOOLTIP} />
              </Label>
              <Textarea
                id="topic-description"
                value={addForm.description}
                onChange={(e) => setAddForm({ ...addForm, description: e.target.value })}
                placeholder="Optional context for the Pulse scoring LLM"
                rows={2}
                maxLength={1000}
              />
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
