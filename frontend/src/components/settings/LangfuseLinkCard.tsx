import { useState } from 'react';
import { ExternalLink } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { fetchConfig, setConfig } from '@/lib/api';
import type { ConfigEntry } from '@/types';
import { useAuthStore } from '@/stores/auth-store';
import { errorMessage } from '@/lib/errors';

const CONFIG_KEY = 'observability.langfuse_dashboard_url';

/**
 * The stored URL is rendered as a user-facing link, so only https:// or a
 * loopback http:// URL (local-dev Langfuse) is treated as safe to open.
 * Mirrors the backend `_validate_langfuse_dashboard_url` guard.
 */
function isSafeDashboardUrl(value: string): boolean {
  try {
    const u = new URL(value);
    if (u.protocol === 'https:') return true;
    return u.protocol === 'http:' && (u.hostname === 'localhost' || u.hostname === '127.0.0.1');
  } catch {
    return false;
  }
}

export function LangfuseLinkCard() {
  const isAdmin = useAuthStore((s) => s.user?.role === 'admin');
  const queryClient = useQueryClient();

  const { data: configs } = useQuery<ConfigEntry[]>({
    queryKey: QUERY_KEYS.config.all(),
    queryFn: fetchConfig,
  });
  const savedUrl = (configs?.find((c) => c.key === CONFIG_KEY)?.value as string | undefined) ?? '';
  const safeSavedUrl = isSafeDashboardUrl(savedUrl) ? savedUrl : null;

  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fieldValue = draft ?? savedUrl;

  const saveMut = useMutation({
    mutationFn: (value: string) => setConfig(CONFIG_KEY, value),
    onSuccess: () => {
      setError(null);
      setDraft(null);
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() });
    },
    onError: (e) => setError(errorMessage(e, 'Failed to save')),
  });

  const onSave = () => {
    const value = fieldValue.trim();
    if (value !== '' && !isSafeDashboardUrl(value)) {
      setError('Must be an https:// URL or an http://localhost / http://127.0.0.1 URL');
      return;
    }
    saveMut.mutate(value);
  };

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardHeader>
        <CardTitle>Observability</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          LLM call traces, latency, and token usage are tracked via Langfuse. Enable the{' '}
          <code>observability</code> Docker Compose profile, then point this at the Langfuse
          dashboard to open it from here.
        </p>

        {safeSavedUrl && (
          <Button asChild variant="outline" size="sm">
            <a href={safeSavedUrl} target="_blank" rel="noreferrer noopener">
              <ExternalLink className="h-4 w-4 mr-2" />
              Open Langfuse dashboard
            </a>
          </Button>
        )}

        {isAdmin ? (
          <div className="space-y-2">
            <Label htmlFor="langfuse-url" className="text-sm">
              Langfuse dashboard URL
            </Label>
            <div className="flex gap-2">
              <Input
                id="langfuse-url"
                placeholder="http://localhost:3002"
                value={fieldValue}
                onChange={(e) => setDraft(e.target.value)}
              />
              <Button size="sm" disabled={saveMut.isPending} onClick={onSave}>
                Save
              </Button>
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
            <p className="text-xs text-muted-foreground">
              Leave empty to clear. Default local dashboard: <code>http://localhost:3002</code>.
            </p>
          </div>
        ) : (
          !safeSavedUrl && (
            <p className="text-sm text-muted-foreground">
              Not configured. Ask an administrator to set the Langfuse dashboard URL.
            </p>
          )
        )}
      </CardContent>
    </Card>
  );
}
