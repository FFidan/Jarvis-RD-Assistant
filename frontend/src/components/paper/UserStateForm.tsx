import { useState, useEffect, useRef } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { upsertUserState } from '@/lib/api';
import type { UserState } from '@/types';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Separator } from '@/components/ui/separator';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

const STATUS_OPTIONS = ['new', 'reading', 'read'] as const;

interface UserStateFormProps {
  paperId: number;
  userState: UserState | null;
}

export function UserStateForm({ paperId, userState }: UserStateFormProps) {
  const queryClient = useQueryClient();
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [status, setStatus] = useState(userState?.status ?? 'new');
  const [rating, setRating] = useState(userState?.rating ?? 3);
  const [notes, setNotes] = useState(userState?.user_notes ?? '');
  const [flagged, setFlagged] = useState(userState?.flagged ?? false);
  const [saved, setSaved] = useState(false);

  // Sync form when userState changes (e.g. after a refetch)
  useEffect(() => {
    setStatus(userState?.status ?? 'new');
    setRating(userState?.rating ?? 3);
    setNotes(userState?.user_notes ?? '');
    setFlagged(userState?.flagged ?? false);
  }, [userState]);

  // Clear pending timer on unmount to avoid setState-after-unmount
  useEffect(
    () => () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
      }
    },
    [],
  );

  const mutation = useMutation({
    mutationFn: () =>
      upsertUserState(paperId, {
        status,
        rating,
        user_notes: notes || null,
        flagged,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['paper-detail', paperId] });
      setSaved(true);
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
      }
      timerRef.current = setTimeout(() => setSaved(false), 2000);
    },
  });

  return (
    <div className="space-y-4">
      <Separator />
      <h3 className="text-lg font-semibold">My Notes</h3>

      <div className="space-y-1">
        <Label htmlFor="user-status">Status</Label>
        <Select value={status} onValueChange={(v) => setStatus(v as 'new' | 'reading' | 'read')}>
          <SelectTrigger id="user-status">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {STATUS_OPTIONS.map((s) => (
              <SelectItem key={s} value={s} className="capitalize">
                {s}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-1">
        <Label htmlFor="user-rating">Rating: {rating}</Label>
        <input
          id="user-rating"
          type="range"
          min={1}
          max={5}
          value={rating}
          onChange={(e) => setRating(Number(e.target.value))}
          className="w-full accent-primary"
        />
        <div className="flex justify-between text-xs text-muted-foreground">
          <span>1</span>
          <span>2</span>
          <span>3</span>
          <span>4</span>
          <span>5</span>
        </div>
      </div>

      <div className="space-y-1">
        <Label htmlFor="user-notes">Notes</Label>
        <Textarea
          id="user-notes"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Your notes about this paper..."
          rows={4}
        />
      </div>

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={flagged}
          onChange={(e) => setFlagged(e.target.checked)}
          className="h-4 w-4 rounded border-gray-300 accent-primary"
        />
        Flagged
      </label>

      <Button
        className="w-full"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending}
      >
        {mutation.isPending ? 'Saving...' : saved ? 'Saved!' : 'Save Notes'}
      </Button>

      {mutation.isError && (
        <p className="text-sm text-destructive">
          {mutation.error instanceof Error ? mutation.error.message : 'Failed to save'}
        </p>
      )}
    </div>
  );
}
