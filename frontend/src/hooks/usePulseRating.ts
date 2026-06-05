import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { QUERY_KEYS } from '@/lib/query-keys';
import { ratePulseCard } from '@/lib/api';
import type { PulseDeck, PulseCardItem, PulseRating } from '@/types';

export interface UsePulseRatingOptions {
  /**
   * Called after a successful rating. Receives the variables so callers can
   * derive component-specific state updates (e.g. advance an index, mark a card rated).
   */
  onSuccess?: (vars: { paperId: number; rating: PulseRating }) => void;
}

/**
 * Shared rating mutation for Pulse cards.
 *
 * Behaviour preserved from both call-sites:
 * - onMutate: optimistically marks the card `user_state: 'to_read'` when rating is 'save'
 * - onError:  reverts the optimistic update and shows a toast
 * - onSettled: invalidates the pulse-today query
 * - onSuccess: delegates to the caller-supplied callback (index advance or rated-set update)
 */
export function usePulseRating({ onSuccess }: UsePulseRatingOptions = {}) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ paperId, rating }: { paperId: number; rating: PulseRating }) =>
      ratePulseCard(paperId, rating),

    onMutate: async ({ paperId, rating }) => {
      if (rating !== 'save') return undefined;
      const prev = queryClient.getQueryData<PulseDeck>(QUERY_KEYS.pulse.today());
      if (prev) {
        queryClient.setQueryData<PulseDeck>(QUERY_KEYS.pulse.today(), {
          ...prev,
          cards: prev.cards.map((c: PulseCardItem) =>
            c.paper_id === paperId ? { ...c, user_state: 'to_read' } : c,
          ),
        });
      }
      return { prev };
    },

    onSuccess: (_data, vars) => {
      onSuccess?.(vars);
    },

    onError: (err: Error, _vars, context) => {
      if (context?.prev !== undefined) {
        queryClient.setQueryData(QUERY_KEYS.pulse.today(), context.prev);
      }
      toast.error(`Failed to rate card: ${err.message ?? 'unknown error'}`);
    },

    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.pulse.today() });
    },
  });
}
