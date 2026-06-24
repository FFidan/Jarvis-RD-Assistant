import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Trash2, CreditCard } from 'lucide-react';
import { toast } from 'sonner';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchCards, deleteCard } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { EmptyState } from '@/components/EmptyState';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { cardTypeLabel } from '@/lib/labels/cardTypes';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

interface CardListProps {
  deckId: number;
}

export function CardList({ deckId }: CardListProps) {
  const queryClient = useQueryClient();
  const [deleteId, setDeleteId] = useState<number | null>(null);

  const { data: cards = [], isLoading } = useQuery({
    queryKey: QUERY_KEYS.cards.byDeck(deckId),
    queryFn: () => fetchCards(deckId),
  });

  const delMut = useMutation({
    mutationFn: (id: number) => deleteCard(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.cards.byDeck(deckId) });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.decks.list() });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.cards.stats() });
      setDeleteId(null);
    },
    onError: () => toast.error('Failed to delete card. Please try again.'),
  });

  if (isLoading) {
    return (
      <div className="space-y-2">
        {[1, 2, 3].map((i) => <Skeleton key={i} className="h-12 w-full" />)}
      </div>
    );
  }

  if (cards.length === 0) {
    return (
      <EmptyState
        title="No cards in this deck"
        description="Generate cards from a paper or create one manually using the buttons above."
        icon={CreditCard}
      />
    );
  }

  return (
    <>
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Front</TableHead>
              <TableHead className="hidden md:table-cell">Back</TableHead>
              <TableHead className="w-[100px]">Type</TableHead>
              <TableHead className="w-[100px]">Due</TableHead>
              <TableHead className="w-[50px]" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {cards.map((card) => (
              <TableRow key={card.id}>
                <TableCell className="max-w-[250px] truncate font-medium">
                  {card.front}
                </TableCell>
                <TableCell className="hidden md:table-cell max-w-[250px] truncate text-muted-foreground">
                  {card.back}
                </TableCell>
                <TableCell>
                  <Badge variant="outline" className="text-xs">
                    {cardTypeLabel(card.card_type)}
                  </Badge>
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {card.due_at
                    ? new Date(card.due_at) <= new Date()
                      ? 'Now'
                      : new Date(card.due_at).toLocaleDateString()
                    : '-'}
                </TableCell>
                <TableCell>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => setDeleteId(card.id)}
                  >
                    <Trash2 className="h-4 w-4 text-muted-foreground" />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      <ConfirmDialog
        open={deleteId !== null}
        title="Delete card?"
        description="This will permanently remove this flashcard."
        confirmLabel="Delete"
        onConfirm={() => deleteId && delMut.mutate(deleteId)}
        onCancel={() => setDeleteId(null)}
      />
    </>
  );
}
