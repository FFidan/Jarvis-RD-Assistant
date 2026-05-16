import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, Library, Download, Play } from 'lucide-react';
import type { Deck } from '@/types';
import { fetchDecks, createDeck, exportAnki } from '@/lib/api';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { EmptyState } from '@/components/EmptyState';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';

interface DeckBrowserProps {
  selectedDeckId: number | null;
  onSelectDeck: (id: number) => void;
  /** Called when user clicks "Start review" on a deck — launches a scoped session. */
  onStartReview?: (deckId: number) => void;
}

export function DeckBrowser({ selectedDeckId, onSelectDeck, onStartReview }: DeckBrowserProps) {
  const queryClient = useQueryClient();
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [exporting, setExporting] = useState<number | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  const { data: decks = [], isLoading } = useQuery({
    queryKey: ['decks'],
    queryFn: fetchDecks,
  });

  const createMut = useMutation({
    mutationFn: () => createDeck({ name, description: description || undefined }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['decks'] });
      setShowCreate(false);
      setName('');
      setDescription('');
    },
  });

  const handleExport = async (deck: Deck) => {
    setExporting(deck.id);
    setExportError(null);
    try {
      await exportAnki(deck.id);
    } catch {
      setExportError('Failed to export deck. Please try again.');
    } finally {
      setExporting(null);
    }
  };

  if (isLoading) {
    return (
      <div className="space-y-3">
        {[1, 2, 3].map((i) => <Skeleton key={i} className="h-20 w-full" />)}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-muted-foreground">
          {decks.length} deck{decks.length !== 1 ? 's' : ''}
        </h3>
        <Button size="sm" onClick={() => setShowCreate(true)}>
          <Plus className="mr-1 h-4 w-4" /> New Deck
        </Button>
      </div>

      {decks.length === 0 ? (
        <EmptyState
          title="No flashcard decks yet"
          description="Generate cards from a paper to start spaced repetition learning, or create a deck manually."
          icon={Library}
          actionLabel="Create Deck"
          onAction={() => setShowCreate(true)}
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {decks.map((deck) => (
            <button
              key={deck.id}
              onClick={() => onSelectDeck(deck.id)}
              className={cn(
                'flex flex-col items-start rounded-lg border p-4 text-left transition-colors hover:bg-accent',
                selectedDeckId === deck.id && 'bg-accent border-primary',
              )}
            >
              <div className="flex w-full items-center justify-between">
                <span className="font-medium truncate">{deck.name}</span>
                <div className="flex items-center gap-1.5 ml-2 shrink-0">
                  {deck.due_count > 0 && onStartReview && (
                    <Button
                      variant="default"
                      size="sm"
                      className="h-7 px-2 text-xs gap-1"
                      onClick={(e) => {
                        e.stopPropagation();
                        onStartReview(deck.id);
                      }}
                      title={`Start review — ${deck.due_count} card${deck.due_count !== 1 ? 's' : ''} due`}
                    >
                      <Play className="h-3 w-3" />
                      Review {deck.due_count}
                    </Button>
                  )}
                  {deck.due_count > 0 && !onStartReview && (
                    <Badge variant="destructive" className="text-xs">
                      {deck.due_count} due
                    </Badge>
                  )}
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={(e) => {
                      e.stopPropagation();
                      handleExport(deck);
                    }}
                    disabled={exporting === deck.id || deck.card_count === 0}
                    title="Export as Anki .apkg"
                  >
                    <Download className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
              {deck.description && (
                <p className="text-xs text-muted-foreground mt-1 truncate w-full">
                  {deck.description}
                </p>
              )}
              <p className="text-xs text-muted-foreground mt-2">
                {deck.card_count} card{deck.card_count !== 1 ? 's' : ''}
              </p>
            </button>
          ))}
        </div>
      )}

      {exportError && <p className="text-sm text-destructive mt-1">{exportError}</p>}

      <Dialog open={showCreate} onOpenChange={setShowCreate}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New Deck</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label htmlFor="deck-name">Name</Label>
              <Input id="deck-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Deck name" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="deck-desc">Description</Label>
              <Input id="deck-desc" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Optional" />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowCreate(false)}>Cancel</Button>
            <Button onClick={() => createMut.mutate()} disabled={!name.trim() || createMut.isPending}>
              {createMut.isPending ? 'Creating...' : 'Create'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
