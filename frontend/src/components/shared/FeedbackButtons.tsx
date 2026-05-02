import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ThumbsUp, ThumbsDown } from 'lucide-react';
import { toast } from 'sonner';
import { submitFeedback, clearFeedback } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { cn } from '@/lib/utils';

// Must align with FeedbackBody['source'] in api.ts (excludes dismiss_combined)
type FeedbackSource = 'feed_thumbs' | 'paper_detail_thumbs' | 'pulse_thumbs';

export interface FeedbackButtonsProps {
  paperId: number;
  discoveryOrigin: 'user_initiated' | 'pulse' | 'recommender' | 'citation_batch';
  source: FeedbackSource;
  recentFeedback?: { signal: 'positive' | 'negative' } | null;
  size?: 'sm' | 'md';
  onSuccess?: () => void;
  className?: string;
  /**
   * When true, after a thumb click an optional free-text reason input slides in
   * (Paper Detail sidebar only — spec §5.2 line 349). The reason is saved by
   * an UPSERT (the immediate thumb click submits without reason; the "Save reason"
   * button updates the existing row via ON CONFLICT (paper_id, user_id, source)
   * DO UPDATE on the backend). Reason is collected for future analysis (spec §10);
   * does not influence L1/L2/L3 today.
   */
  showReasonInput?: boolean;
}

export function FeedbackButtons({
  paperId,
  discoveryOrigin,
  source,
  recentFeedback,
  size = 'sm',
  onSuccess,
  className,
  showReasonInput = false,
}: FeedbackButtonsProps) {
  // Spec §5.2 origin gate — hidden for user-initiated papers
  if (discoveryOrigin === 'user_initiated') return null;

  const queryClient = useQueryClient();
  const [reason, setReason] = useState('');
  const [reasonOpen, setReasonOpen] = useState(false);
  const [lastSignal, setLastSignal] = useState<'positive' | 'negative' | null>(null);

  const mutation = useMutation({
    mutationFn: ({ signal, reason: reasonText }: { signal: 'positive' | 'negative'; reason?: string }) =>
      submitFeedback(paperId, { signal, source, reason: reasonText || undefined }),
    onSuccess: () => onSuccess?.(),
    onError: (err) =>
      toast.error('Failed to record feedback', {
        description: err instanceof Error ? err.message : 'Unknown error',
      }),
  });

  const clearMutation = useMutation({
    mutationFn: () => clearFeedback(paperId, source),
    onSuccess: () => {
      setLastSignal(null);
      void queryClient.invalidateQueries({ queryKey: ['recent-feedback', paperId] });
      onSuccess?.();
    },
    onError: (err) =>
      toast.error('Failed to clear feedback', {
        description: err instanceof Error ? err.message : 'Unknown error',
      }),
  });

  const handleThumb = (signal: 'positive' | 'negative') => {
    const currentSignal = recentFeedback?.signal ?? lastSignal;
    if (signal === currentSignal) {
      // Clicking the already-active button → untoggle (clear)
      clearMutation.mutate();
      return;
    }
    setLastSignal(signal);
    // Defer reason-textarea reveal until the first POST succeeds — avoids a
    // race where the user clicks "Save reason" while the initial thumb call
    // is still in flight, which would emit a doubled toast pair.
    mutation.mutate(
      { signal },
      {
        onSuccess: () => {
          if (showReasonInput) setReasonOpen(true);
        },
      },
    );
  };

  const handleSaveReason = () => {
    if (!lastSignal) return;
    mutation.mutate(
      { signal: lastSignal, reason: reason.trim() },
      {
        onSuccess: () => {
          toast.success('Reason saved');
          setReasonOpen(false);
          setReason('');
          onSuccess?.();
        },
      },
    );
  };

  // After a successful untoggle, clearMutation sets lastSignal to null and
  // invalidates the query — recentFeedback will flip to null on refetch.
  const positiveActive = (recentFeedback?.signal === 'positive' || lastSignal === 'positive') && !clearMutation.isSuccess;
  const negativeActive = (recentFeedback?.signal === 'negative' || lastSignal === 'negative') && !clearMutation.isSuccess;

  const isPending = mutation.isPending || clearMutation.isPending;
  const iconSize = size === 'md' ? 18 : 14;

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <div className="flex items-center gap-1">
        <Button
          type="button"
          variant={positiveActive ? 'default' : 'ghost'}
          size="sm"
          disabled={isPending}
          onClick={() => handleThumb('positive')}
          aria-label="Recommend more like this"
          title="More like this"
        >
          <ThumbsUp size={iconSize} />
        </Button>
        <Button
          type="button"
          variant={negativeActive ? 'default' : 'ghost'}
          size="sm"
          disabled={isPending}
          onClick={() => handleThumb('negative')}
          aria-label="Don't recommend like this"
          title="Less like this"
        >
          <ThumbsDown size={iconSize} />
        </Button>
      </div>

      {showReasonInput && reasonOpen && (
        <div className="flex flex-col gap-1.5">
          <Textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={
              lastSignal === 'negative'
                ? "Optional: why isn't this useful? (helps future tuning)"
                : 'Optional: what made this a good fit?'
            }
            className="min-h-[60px] text-xs"
            rows={2}
          />
          <div className="flex justify-end gap-1.5">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => {
                setReasonOpen(false);
                setReason('');
              }}
            >
              Skip
            </Button>
            <Button
              type="button"
              variant="default"
              size="sm"
              disabled={mutation.isPending || !reason.trim() || !lastSignal}
              onClick={handleSaveReason}
            >
              Save reason
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
