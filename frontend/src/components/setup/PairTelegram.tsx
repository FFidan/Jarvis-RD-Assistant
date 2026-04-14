import { useCallback, useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, CheckCircle2, Loader2, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { createPairingCode, getPairingStatus, unpairTelegram } from '@/lib/api';
import type { TelegramPairing } from '@/types';

type PairState =
  | { kind: 'idle' }
  | { kind: 'polling'; pairing: TelegramPairing }
  | { kind: 'paired'; chatId: number | null }
  | { kind: 'bot_missing'; pairing: TelegramPairing }
  | { kind: 'error'; message: string };

interface PairTelegramProps {
  onPaired?: () => void;
}

/**
 * Reusable Telegram pairing UI.
 *
 * Transitions:
 *   idle -> (click) -> polling | bot_missing | error
 *   polling -> (server confirms) -> paired -> (unpair) -> idle
 *   polling -> (code expires) -> idle with warning
 */
export function PairTelegram({ onPaired }: PairTelegramProps) {
  const queryClient = useQueryClient();
  const [state, setState] = useState<PairState>({ kind: 'idle' });
  const [expiredNotice, setExpiredNotice] = useState(false);
  const expiryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onPairedRef = useRef(onPaired);

  useEffect(() => {
    onPairedRef.current = onPaired;
  }, [onPaired]);

  // Seed initial state from the server: if we're already paired, jump straight there.
  const initialStatus = useQuery({
    queryKey: ['pairing-status-initial'],
    queryFn: getPairingStatus,
    staleTime: 0,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    if (initialStatus.data?.paired && state.kind === 'idle') {
      setState({ kind: 'paired', chatId: initialStatus.data.chat_id });
    }
  }, [initialStatus.data, state.kind]);

  const clearExpiryTimer = useCallback(() => {
    if (expiryTimerRef.current !== null) {
      clearTimeout(expiryTimerRef.current);
      expiryTimerRef.current = null;
    }
  }, []);

  useEffect(() => clearExpiryTimer, [clearExpiryTimer]);

  const createMut = useMutation({
    mutationFn: createPairingCode,
    onSuccess: (pairing) => {
      setExpiredNotice(false);
      if (pairing.bot_username_missing) {
        setState({ kind: 'bot_missing', pairing });
        return;
      }
      setState({ kind: 'polling', pairing });
      // Schedule auto-reset when the code expires.
      clearExpiryTimer();
      const expiresAtMs = Date.parse(pairing.expires_at);
      const delay = Number.isFinite(expiresAtMs)
        ? Math.max(0, expiresAtMs - Date.now())
        : 10 * 60 * 1000;
      expiryTimerRef.current = setTimeout(() => {
        setExpiredNotice(true);
        setState({ kind: 'idle' });
      }, delay);
    },
    onError: (err: Error) => {
      console.error('Failed to create Telegram pairing code', err);
      setState({ kind: 'error', message: err.message || 'Failed to create pairing code' });
    },
  });

  // Poll the pairing status while in polling state.
  const polling = state.kind === 'polling';
  const statusQuery = useQuery({
    queryKey: ['pairing-status'],
    queryFn: getPairingStatus,
    refetchInterval: 3000,
    enabled: polling,
    staleTime: 0,
  });

  useEffect(() => {
    if (state.kind !== 'polling') return;
    if (statusQuery.data?.paired) {
      clearExpiryTimer();
      setState({ kind: 'paired', chatId: statusQuery.data.chat_id });
      onPairedRef.current?.();
      queryClient.invalidateQueries({ queryKey: ['setup-status'] });
    }
  }, [statusQuery.data, state.kind, clearExpiryTimer, queryClient]);

  const unpairMut = useMutation({
    mutationFn: unpairTelegram,
    onSuccess: () => {
      setState({ kind: 'idle' });
      queryClient.invalidateQueries({ queryKey: ['setup-status'] });
      queryClient.invalidateQueries({ queryKey: ['pairing-status-initial'] });
    },
    onError: (err: Error) => {
      console.error('Failed to unpair Telegram', err);
    },
  });

  if (state.kind === 'paired') {
    return (
      <div className="space-y-3">
        <div className="flex items-center gap-2 rounded-md border border-green-500/40 bg-green-500/10 p-3 text-sm">
          <CheckCircle2 className="h-5 w-5 text-green-500" />
          <span>
            Paired with chat ID <span className="font-mono">{state.chatId ?? 'unknown'}</span>
          </span>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => unpairMut.mutate()}
          disabled={unpairMut.isPending}
        >
          {unpairMut.isPending ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : null}
          Unpair
        </Button>
      </div>
    );
  }

  if (state.kind === 'bot_missing') {
    return (
      <div className="space-y-3">
        <div className="flex items-start gap-2 rounded-md border border-yellow-500/40 bg-yellow-500/10 p-3 text-sm">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-yellow-500" />
          <div>
            <p className="font-medium">Bot username unknown</p>
            <p className="text-muted-foreground">
              Ensure <span className="font-mono">TELEGRAM_BOT_TOKEN</span> is set in{' '}
              <span className="font-mono">.env</span> and restart paper_ingestion, then generate
              a new code.
            </p>
            <p className="mt-2 text-xs text-muted-foreground">
              Pairing code:{' '}
              <span className="font-mono">{state.pairing.code}</span>
            </p>
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={() => setState({ kind: 'idle' })}>
          <RefreshCw className="mr-2 h-4 w-4" />
          Try again
        </Button>
      </div>
    );
  }

  if (state.kind === 'polling') {
    const isValidTelegramLink = (url: string): boolean => /^https:\/\/t\.me\//.test(url);

    return (
      <div className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Tap the link below on the device running Telegram, or open the bot manually and
          send <span className="font-mono">/start PAIR_{state.pairing.code}</span>.
        </p>
        <div className="space-y-2">
          <div>
            <p className="text-xs uppercase text-muted-foreground">Pairing code</p>
            <p className="font-mono text-2xl tracking-widest">{state.pairing.code}</p>
          </div>
          {state.pairing.deep_link && isValidTelegramLink(state.pairing.deep_link) ? (
            <a
              href={state.pairing.deep_link}
              className="inline-flex items-center text-sm text-primary underline"
              target="_blank"
              rel="noopener noreferrer"
            >
              Open in Telegram
            </a>
          ) : state.pairing.deep_link ? (
            <p className="text-sm text-destructive">Invalid pairing link</p>
          ) : null}
        </div>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" />
          Waiting for confirmation...
        </div>
      </div>
    );
  }

  // idle / error
  return (
    <div className="space-y-3">
      {expiredNotice && (
        <div className="rounded-md border border-yellow-500/40 bg-yellow-500/10 p-2 text-xs">
          Code expired — generate a new one.
        </div>
      )}
      {state.kind === 'error' && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
          {state.message}
        </div>
      )}
      <Button
        onClick={() => createMut.mutate()}
        disabled={createMut.isPending}
      >
        {createMut.isPending ? (
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        ) : null}
        Generate pairing code
      </Button>
    </div>
  );
}
