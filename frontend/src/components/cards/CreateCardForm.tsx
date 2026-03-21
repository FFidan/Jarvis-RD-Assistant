import { useState, useEffect } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Sparkles } from 'lucide-react';
import { createCard, generateCards, fetchDecks } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { PaperSearchSelect } from '@/components/shared/PaperSearchSelect';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

const CARD_TYPES = ['concept', 'quote', 'method', 'comparison'] as const;

interface CreateCardFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaultDeckId?: number | null;
}

export function CreateCardForm({ open, onOpenChange, defaultDeckId }: CreateCardFormProps) {
  const queryClient = useQueryClient();
  const [front, setFront] = useState('');
  const [back, setBack] = useState('');
  const [cardType, setCardType] = useState<string>('concept');
  const [deckId, setDeckId] = useState<string>(defaultDeckId ? String(defaultDeckId) : '');

  const { data: decks = [] } = useQuery({
    queryKey: ['decks'],
    queryFn: fetchDecks,
  });

  const createMut = useMutation({
    mutationFn: () =>
      createCard({
        deck_id: Number(deckId),
        card_type: cardType,
        front,
        back,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['cards'] });
      queryClient.invalidateQueries({ queryKey: ['decks'] });
      queryClient.invalidateQueries({ queryKey: ['card-stats'] });
      onOpenChange(false);
      setFront('');
      setBack('');
      setCardType('concept');
    },
  });

  // Sync default deck when it changes
  useEffect(() => {
    if (defaultDeckId && !deckId) {
      setDeckId(String(defaultDeckId));
    }
  }, [defaultDeckId]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle>Create Card</DialogTitle>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div className="space-y-2">
            <Label htmlFor="card-deck">Deck</Label>
            <Select value={deckId} onValueChange={setDeckId}>
              <SelectTrigger id="card-deck">
                <SelectValue placeholder="Select a deck" />
              </SelectTrigger>
              <SelectContent>
                {decks.map((d) => (
                  <SelectItem key={d.id} value={String(d.id)}>
                    {d.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="card-type">Type</Label>
            <Select value={cardType} onValueChange={setCardType}>
              <SelectTrigger id="card-type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CARD_TYPES.map((t) => (
                  <SelectItem key={t} value={t} className="capitalize">
                    {t}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="card-front">Front (Question)</Label>
            <Textarea
              id="card-front"
              value={front}
              onChange={(e) => setFront(e.target.value)}
              placeholder="What is the question?"
              rows={3}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="card-back">Back (Answer)</Label>
            <Textarea
              id="card-back"
              value={back}
              onChange={(e) => setBack(e.target.value)}
              placeholder="What is the answer?"
              rows={3}
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button
            onClick={() => createMut.mutate()}
            disabled={!front.trim() || !back.trim() || !deckId || createMut.isPending}
          >
            {createMut.isPending ? 'Creating...' : 'Create'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// --- Generate Cards Dialog ---

interface GenerateCardsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaultDeckId?: number | null;
}

export function GenerateCardsDialog({ open, onOpenChange, defaultDeckId }: GenerateCardsDialogProps) {
  const queryClient = useQueryClient();
  const [paperId, setPaperId] = useState('');
  const [deckId, setDeckId] = useState<string>(defaultDeckId ? String(defaultDeckId) : '');
  const [maxCards, setMaxCards] = useState('5');
  const [result, setResult] = useState<string | null>(null);

  const { data: decks = [] } = useQuery({
    queryKey: ['decks'],
    queryFn: fetchDecks,
  });

  const genMut = useMutation({
    mutationFn: () => generateCards(Number(paperId), Number(deckId), Number(maxCards)),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['cards'] });
      queryClient.invalidateQueries({ queryKey: ['decks'] });
      queryClient.invalidateQueries({ queryKey: ['card-stats'] });
      setResult(`Generated ${data.cards_created} cards (confidence: ${data.confidence})`);
    },
    onError: (err) => {
      setResult(`Error: ${err instanceof Error ? err.message : 'Generation failed'}`);
    },
  });

  // Sync default deck when it changes
  useEffect(() => {
    if (defaultDeckId && !deckId) {
      setDeckId(String(defaultDeckId));
    }
  }, [defaultDeckId]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Dialog open={open} onOpenChange={(v) => { onOpenChange(v); if (!v) setResult(null); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5" /> Generate Cards from Paper
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div className="space-y-2">
            <Label>Paper</Label>
            <PaperSearchSelect
              value={paperId ? Number(paperId) : null}
              onChange={(id) => setPaperId(id ? String(id) : '')}
              placeholder="Search for a paper..."
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="gen-deck">Deck</Label>
            <Select value={deckId} onValueChange={setDeckId}>
              <SelectTrigger id="gen-deck">
                <SelectValue placeholder="Select a deck" />
              </SelectTrigger>
              <SelectContent>
                {decks.map((d) => (
                  <SelectItem key={d.id} value={String(d.id)}>
                    {d.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="gen-max">Max Cards</Label>
            <Input
              id="gen-max"
              type="number"
              min={1}
              max={20}
              value={maxCards}
              onChange={(e) => setMaxCards(e.target.value)}
            />
          </div>
          {result && (
            <p className={`text-sm ${result.startsWith('Error') ? 'text-destructive' : 'text-green-600'}`}>
              {result}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => { onOpenChange(false); setResult(null); }}>
            {result ? 'Close' : 'Cancel'}
          </Button>
          {!result && (
            <Button
              onClick={() => genMut.mutate()}
              disabled={!paperId || !deckId || genMut.isPending}
            >
              {genMut.isPending ? 'Generating...' : 'Generate'}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
