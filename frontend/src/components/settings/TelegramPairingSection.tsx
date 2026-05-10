/**
 * TelegramPairingSection — per-user multi-tenant Telegram pairing UI (Sprint A).
 *
 * Shows:
 *  - Current pairing status (paired / unpaired)
 *  - Button to request a 15-minute pairing token
 *  - Token display with copy button and live countdown
 *  - Unpair button when paired
 *
 * Backed by:
 *  POST   /api/telegram/pair-token  → requestTelegramPairToken()
 *  GET    /api/telegram/pairing     → getTelegramPairing()
 *  DELETE /api/telegram/pairing     → removeTelegramPairing()
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, Copy, Loader2, Unlink } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { getTelegramPairing, removeTelegramPairing, requestTelegramPairToken } from '@/lib/api';
import type { TelegramPairTokenResponse } from '@/lib/api';

// ---------------------------------------------------------------------------
// Countdown hook
// ---------------------------------------------------------------------------

function useCountdown(expiresAt: string | null): string {
  const [remaining, setRemaining] = useState('');

  useEffect(() => {
    if (!expiresAt) {
      setRemaining('');
      return;
    }
    const expiresMs = Date.parse(expiresAt);
    const tick = () => {
      const diff = expiresMs - Date.now();
      if (diff <= 0) {
        setRemaining('expired');
        return;
      }
      const mins = Math.floor(diff / 60_000);
      const secs = Math.floor((diff % 60_000) / 1000);
      setRemaining(`${mins}:${secs.toString().padStart(2, '0')}`);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [expiresAt]);

  return remaining;
}

// ---------------------------------------------------------------------------
// Token display sub-component
// ---------------------------------------------------------------------------

function TokenDisplay({ token, expiresAt, onExpired }: {
  token: string;
  expiresAt: string;
  onExpired: () => void;
}) {
  const countdown = useCountdown(expiresAt);
  const [copied, setCopied] = useState(false);
  const onExpiredRef = useRef(onExpired);
  useEffect(() => { onExpiredRef.current = onExpired; }, [onExpired]);

  useEffect(() => {
    if (countdown === 'expired') onExpiredRef.current();
  }, [countdown]);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard permission denied — silently ignore */
    }
  }, [token]);

  const isExpired = countdown === 'expired';

  return (
    <div className="rounded-md border border-hair bg-muted/30 p-4 space-y-3">
      <div>
        <p className="text-xs uppercase tracking-wide text-muted-foreground mb-1">
          Pairing token
        </p>
        <div className="flex items-center gap-2">
          <code className={`font-mono text-lg tracking-widest select-all ${isExpired ? 'text-muted-foreground line-through' : ''}`}>
            {token}
          </code>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 shrink-0"
            onClick={handleCopy}
            disabled={isExpired}
            title="Copy token"
          >
            <Copy className="h-3.5 w-3.5" />
          </Button>
          {copied && (
            <span className="text-xs text-green-500">Copied!</span>
          )}
        </div>
      </div>
      <p className="text-sm text-muted-foreground">
        In Telegram, send{' '}
        <code className="font-mono">/pair {token}</code>{' '}
        to your JARVIS bot.
      </p>
      <div className="flex items-center gap-2 text-xs">
        {isExpired ? (
          <span className="text-destructive">Token expired — generate a new one.</span>
        ) : (
          <span className="text-muted-foreground">
            Expires in{' '}
            <span className={`font-mono ${countdown.startsWith('0:') ? 'text-orange-500' : ''}`}>
              {countdown}
            </span>
          </span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main section
// ---------------------------------------------------------------------------

export function TelegramPairingSection() {
  const queryClient = useQueryClient();
  const [pendingToken, setPendingToken] = useState<TelegramPairTokenResponse | null>(null);

  // Current pairing status (poll every 5s while a token is pending)
  const statusQuery = useQuery({
    queryKey: ['user-telegram-pairing'],
    queryFn: getTelegramPairing,
    refetchInterval: pendingToken ? 5000 : false,
    staleTime: 0,
  });

  // Detect when pairing is confirmed while token is displayed
  const pairedJustNow = pendingToken !== null && statusQuery.data?.paired === true;
  useEffect(() => {
    if (pairedJustNow) {
      setPendingToken(null);
    }
  }, [pairedJustNow]);

  const requestToken = useMutation({
    mutationFn: requestTelegramPairToken,
    onSuccess: (data) => {
      setPendingToken(data);
    },
  });

  const unpairMut = useMutation({
    mutationFn: removeTelegramPairing,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['user-telegram-pairing'] });
    },
  });

  const handleExpired = useCallback(() => {
    setPendingToken(null);
  }, []);

  const pairing = statusQuery.data;
  const isPaired = pairing?.paired === true;

  if (statusQuery.isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading pairing status…
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-medium">Your Telegram</h3>
        <p className="text-sm text-muted-foreground mt-0.5">
          Link your personal Telegram chat to receive notifications scoped to your account.
        </p>
      </div>

      {/* Paired state */}
      {isPaired && !pendingToken && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 rounded-md border border-green-500/40 bg-green-500/10 p-3 text-sm">
            <CheckCircle2 className="h-5 w-5 shrink-0 text-green-500" />
            <div>
              <span className="font-medium">Paired</span>
              {pairing?.telegram_username && (
                <span className="ml-1 text-muted-foreground">
                  (@{pairing.telegram_username})
                </span>
              )}
              {pairing?.chat_id && (
                <span className="ml-2 font-mono text-xs text-muted-foreground">
                  chat {pairing.chat_id}
                </span>
              )}
            </div>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => unpairMut.mutate()}
            disabled={unpairMut.isPending}
          >
            {unpairMut.isPending ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Unlink className="mr-2 h-4 w-4" />
            )}
            Unpair
          </Button>
        </div>
      )}

      {/* Token display (pending pairing) */}
      {pendingToken && (
        <div className="space-y-3">
          <TokenDisplay
            token={pendingToken.token}
            expiresAt={pendingToken.expires_at}
            onExpired={handleExpired}
          />
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" />
            Waiting for bot confirmation…
          </div>
        </div>
      )}

      {/* Request token button (unpaired + no pending token) */}
      {!isPaired && !pendingToken && (
        <div className="space-y-2">
          {requestToken.isError && (
            <p className="text-sm text-destructive">
              Failed to generate token — please try again.
            </p>
          )}
          <Button
            onClick={() => requestToken.mutate()}
            disabled={requestToken.isPending}
            size="sm"
          >
            {requestToken.isPending ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : null}
            Generate pairing token
          </Button>
        </div>
      )}

      {/* Re-generate button while token is displayed (in case user wants a fresh one) */}
      {pendingToken && (
        <Button
          variant="outline"
          size="sm"
          onClick={() => requestToken.mutate()}
          disabled={requestToken.isPending}
        >
          {requestToken.isPending ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : null}
          Regenerate token
        </Button>
      )}
    </div>
  );
}
