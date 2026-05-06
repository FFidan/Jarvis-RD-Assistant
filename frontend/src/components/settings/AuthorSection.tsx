import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  fetchTrackedAuthors,
  createTrackedAuthor,
  updateTrackedAuthor,
  deleteTrackedAuthor,
  autoDetectAuthors,
  checkTrackedAuthors,
} from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { EmptyState } from '@/components/EmptyState';
import { useConfirm } from '@/hooks/use-confirm';
import { Trash2, Plus, UserSearch, RefreshCw, Users } from 'lucide-react';
import type { TrackedAuthor } from '@/types';
import { InfoTooltip } from '@/components/ui/info-tooltip';

const sourceBadges: Record<string, string> = {
  manual: 'Manual',
  auto_starred: 'Starred',
  auto_rated: 'Rated',
};

export function AuthorSection() {
  const queryClient = useQueryClient();
  const { isOpen, confirm, handleConfirm, handleCancel } = useConfirm();
  const [showAdd, setShowAdd] = useState(false);
  const [addForm, setAddForm] = useState({ author_name: '', s2_author_id: '' });
  const [deleteTarget, setDeleteTarget] = useState<number | null>(null);

  const { data: authors = [], isLoading } = useQuery({
    queryKey: ['tracked-authors'],
    queryFn: fetchTrackedAuthors,
  });

  const createMut = useMutation({
    mutationFn: (data: Partial<TrackedAuthor>) => createTrackedAuthor(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tracked-authors'] });
      setShowAdd(false);
      setAddForm({ author_name: '', s2_author_id: '' });
    },
  });

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<TrackedAuthor> }) =>
      updateTrackedAuthor(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tracked-authors'] });
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteTrackedAuthor(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tracked-authors'] });
    },
  });

  const autoDetectMut = useMutation({
    mutationFn: autoDetectAuthors,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tracked-authors'] });
    },
  });

  const checkMut = useMutation({
    mutationFn: checkTrackedAuthors,
  });

  const handleToggle = (author: TrackedAuthor) => {
    updateMut.mutate({ id: author.id, data: { enabled: !author.enabled } });
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
    if (!addForm.author_name.trim()) return;
    createMut.mutate({
      author_name: addForm.author_name.trim(),
      s2_author_id: addForm.s2_author_id.trim() || undefined,
    } as Partial<TrackedAuthor>);
  };

  if (isLoading) {
    return <div className="py-8 text-center text-muted-foreground">Loading authors...</div>;
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground mb-4">
        Tracked authors receive a score bonus in the Pulse discovery pipeline — papers co-authored by anyone on this list rank higher in your daily Pulse deck.
      </p>
      <div className="flex flex-wrap gap-2">
        <div className="flex items-center gap-1">
          <Button
            variant="outline"
            size="sm"
            onClick={() => autoDetectMut.mutate()}
            disabled={autoDetectMut.isPending}
          >
            <UserSearch className="mr-2 h-4 w-4" />
            {autoDetectMut.isPending ? 'Detecting...' : 'Auto-detect from starred/rated'}
          </Button>
          <InfoTooltip content="Scans the authors of papers you've starred or rated, and suggests frequently-appearing names for tracking." />
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => checkMut.mutate()}
          disabled={checkMut.isPending}
        >
          <RefreshCw className="mr-2 h-4 w-4" />
          {checkMut.isPending ? 'Checking...' : 'Check now'}
        </Button>
      </div>

      {autoDetectMut.isSuccess && (
        <p className="text-sm text-[var(--status-ok)]">
          Added {autoDetectMut.data.added} authors ({autoDetectMut.data.already_tracked} already tracked).
        </p>
      )}
      {checkMut.isSuccess && (
        <p className="text-sm text-[var(--status-ok)]">
          Checked {checkMut.data.authors_checked} authors, found {checkMut.data.new_papers} new papers.
        </p>
      )}

      {authors.length === 0 && !showAdd ? (
        <EmptyState title="No tracked authors" description="Add authors to track their new publications." icon={Users} />
      ) : (
        <div className="space-y-2">
          {authors.map((author) => (
            <Card key={author.id}>
              <CardContent className="flex items-center gap-4 p-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{author.author_name}</span>
                    <Badge variant="secondary">
                      {sourceBadges[author.source] ?? author.source}
                    </Badge>
                    {author.source === 'manual' && (
                      <InfoTooltip content="You added this author manually. Authors detected automatically show an 'Auto' badge." />
                    )}
                    <Badge variant={author.enabled ? 'default' : 'outline'}>
                      {author.enabled ? 'Enabled' : 'Disabled'}
                    </Badge>
                  </div>
                  {author.s2_author_id && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      S2 ID: {author.s2_author_id}
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-1">
                  <Button size="sm" variant="ghost" onClick={() => handleToggle(author)}>
                    {author.enabled ? 'Disable' : 'Enable'}
                  </Button>
                  <Button size="icon" variant="ghost" onClick={() => handleDelete(author.id)} aria-label="Delete author">
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {showAdd ? (
        <Card>
          <CardContent className="space-y-3 p-4">
            <div className="grid gap-2 sm:grid-cols-2">
              <div>
                <Label htmlFor="author-name">Author Name</Label>
                <Input
                  id="author-name"
                  value={addForm.author_name}
                  onChange={(e) => setAddForm({ ...addForm, author_name: e.target.value })}
                  placeholder="e.g. Yoshua Bengio"
                />
              </div>
              <div>
                <Label htmlFor="author-s2">Semantic Scholar ID (optional)</Label>
                <Input
                  id="author-s2"
                  value={addForm.s2_author_id}
                  onChange={(e) => setAddForm({ ...addForm, s2_author_id: e.target.value })}
                  placeholder="e.g. 1234567"
                />
              </div>
            </div>
            <div className="flex gap-2">
              <Button onClick={handleAdd} disabled={createMut.isPending}>
                Add Author
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
          Add Author
        </Button>
      )}

      <ConfirmDialog
        open={isOpen && deleteTarget !== null}
        title="Delete Author"
        description="This will stop tracking this author. Are you sure?"
        confirmLabel="Delete"
        onConfirm={handleConfirm}
        onCancel={handleCancel}
      />
    </div>
  );
}
