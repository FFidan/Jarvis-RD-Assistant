/**
 * TelegramBotTokenSection — admin card for configuring the system Telegram bot token.
 *
 * Shows:
 *  - Whether a bot token is currently stored (has_token status; value never returned).
 *  - A masked input (type=password) to enter a new token.
 *  - Client-side format hint: token must match ^\d+:[A-Za-z0-9_-]{20,}$
 *  - Save button with success/error feedback.
 *  - Persistent note that the Telegram bot must be restarted for the token to take effect.
 *
 * Backed by:
 *  GET  /api/setup/telegram-bot-token → getTelegramBotToken()
 *  POST /api/setup/telegram-bot-token → saveTelegramBotToken(token)
 */

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getTelegramBotToken, saveTelegramBotToken } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardHeader } from '@/components/ui/card';

/** Validates a Telegram bot token format: <bot_id>:<token_string> */
const BOT_TOKEN_RE = /^\d+:[A-Za-z0-9_-]{20,}$/;

export function TelegramBotTokenSection() {
  const qc = useQueryClient();
  const [token, setToken] = useState('');
  const [formatError, setFormatError] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ['telegram-bot-token-status'],
    queryFn: getTelegramBotToken,
    staleTime: 60_000,
  });

  const saveMut = useMutation({
    mutationFn: saveTelegramBotToken,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['telegram-bot-token-status'] });
      setToken('');
    },
  });

  const handleSave = () => {
    const trimmed = token.trim();
    if (!trimmed) return;
    if (!BOT_TOKEN_RE.test(trimmed)) {
      setFormatError(
        'Invalid format. Expected: <bot_id>:<token> (e.g. 123456789:ABCdef…). Token must be at least 20 characters after the colon.',
      );
      return;
    }
    setFormatError(null);
    saveMut.mutate(trimmed);
  };

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading bot token status…</p>;
  }

  const hasToken = data?.has_token ?? false;

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardHeader>
        <p className="text-sm text-muted-foreground">
          The system Telegram bot token identifies which bot JARVIS sends notifications through.
          The stored value is encrypted and never shown.
        </p>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Current token status */}
        <div className="flex items-center gap-2 text-sm">
          <span className="font-medium">Status:</span>
          {hasToken ? (
            <span className="text-green-600 dark:text-green-400">A bot token is configured</span>
          ) : (
            <span className="text-muted-foreground">No bot token set</span>
          )}
        </div>

        {/* Token input */}
        <div className="space-y-1.5">
          <Label htmlFor="telegram-bot-token">
            {hasToken ? 'Replace bot token' : 'Bot token'}
          </Label>
          <Input
            id="telegram-bot-token"
            type="password"
            value={token}
            onChange={(e) => {
              setToken(e.target.value);
              setFormatError(null);
            }}
            placeholder="123456789:ABCdefGHIjkl…"
            autoComplete="off"
          />
          <p className="text-xs text-muted-foreground">
            Format: <code className="font-mono">&lt;bot_id&gt;:&lt;token&gt;</code> — obtain from{' '}
            <span className="font-medium">@BotFather</span> on Telegram.
          </p>
        </div>

        {/* Format error */}
        {formatError && (
          <p className="text-sm text-destructive">{formatError}</p>
        )}

        {/* Server-side error */}
        {saveMut.isError && !formatError && (
          <p className="text-sm text-destructive">
            Could not save:{' '}
            {saveMut.error instanceof Error ? saveMut.error.message : 'unknown error'}
          </p>
        )}

        {/* Save button + success feedback */}
        <div className="flex flex-wrap items-center gap-3">
          <Button
            onClick={handleSave}
            disabled={saveMut.isPending || !token.trim()}
          >
            {saveMut.isPending ? 'Saving…' : 'Save'}
          </Button>

          {saveMut.isSuccess && saveMut.data?.saved && (
            <p className="text-sm text-green-600 dark:text-green-400">
              Token saved.
            </p>
          )}
        </div>

        {/* Persistent restart note */}
        <p className="text-xs text-muted-foreground border-t border-hair pt-3">
          The Telegram bot must be restarted by an administrator for a new token to take effect.
        </p>
      </CardContent>
    </Card>
  );
}
