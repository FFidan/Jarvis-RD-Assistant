import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchConfig, setProviderKey, testProvider } from '@/lib/api';
import type { CloudProvider } from '@/lib/api';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Loader2, CheckCircle, CircleDashed, ShieldAlert } from 'lucide-react';
import { toast } from 'sonner';
import { errorMessage } from '@/lib/errors';
import type { ConfigEntry } from '@/types';

const PROVIDER_LABELS: Record<CloudProvider, string> = {
  anthropic: 'Anthropic (Claude)',
  openai: 'OpenAI (GPT / o-series)',
  google: 'Google (Gemini)',
};

const PROVIDER_PLACEHOLDERS: Record<CloudProvider, string> = {
  anthropic: 'sk-ant-…',
  openai: 'sk-…',
  google: 'AIza…',
};

const PROVIDERS: CloudProvider[] = ['anthropic', 'openai', 'google'];

function getMaskedKey(configs: ConfigEntry[], provider: CloudProvider): string {
  const entry = configs.find((c) => c.key === `llm.${provider}.api_key`);
  if (entry == null) return '';
  const v = entry.value;
  if (typeof v === 'string') return v.replace(/^"|"$/g, '');
  return '';
}

type TestState = { ok: boolean; error: string | null } | null;

function providerStatus(maskedValue: string, result: TestState) {
  if (result?.ok) {
    return {
      icon: CheckCircle,
      label: maskedValue ? 'Configured and tested' : 'Tested',
      className: 'text-[var(--status-ok)]',
    };
  }
  if (result && !result.ok) {
    return {
      icon: ShieldAlert,
      label: `Configured, degraded${result.error ? `: ${result.error}` : ''}`,
      className: 'text-destructive',
    };
  }
  if (!maskedValue) {
    return {
      icon: CircleDashed,
      label: 'Not configured',
      className: 'text-muted-foreground',
    };
  }
  return {
    icon: CircleDashed,
    label: 'Configured, not tested',
    className: 'text-[var(--status-warn)]',
  };
}

export function ProvidersSection() {
  const queryClient = useQueryClient();

  const { data: configs = [], isLoading } = useQuery({
    queryKey: QUERY_KEYS.config.all(),
    queryFn: fetchConfig,
  });

  // Per-provider draft state (null = not editing; string = user has typed something)
  const [drafts, setDrafts] = useState<Partial<Record<CloudProvider, string | null>>>({});
  const [testing, setTesting] = useState<Partial<Record<CloudProvider, boolean>>>({});
  const [testResults, setTestResults] = useState<Partial<Record<CloudProvider, TestState>>>({});

  const setMut = useMutation({
    mutationFn: ({ provider, value }: { provider: CloudProvider; value: string }) =>
      setProviderKey(provider, value),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() });
    },
    onError: (err: Error) => {
      toast.error(err.message ?? 'Failed to save key');
    },
  });

  const handleChange = (provider: CloudProvider, value: string) => {
    setDrafts((prev) => ({ ...prev, [provider]: value }));
  };

  const handleBlur = (provider: CloudProvider) => {
    const draft = drafts[provider];
    const current = getMaskedKey(configs, provider);
    if (draft !== null && draft !== undefined && draft !== current) {
      setMut.mutate({ provider, value: draft });
    }
    setDrafts((prev) => ({ ...prev, [provider]: null }));
  };

  const handleTest = async (provider: CloudProvider) => {
    setTesting((prev) => ({ ...prev, [provider]: true }));
    setTestResults((prev) => ({ ...prev, [provider]: null }));
    try {
      const result = await testProvider(provider);
      setTestResults((prev) => ({ ...prev, [provider]: result }));
      if (result.ok) {
        toast.success(`${PROVIDER_LABELS[provider]} connection OK`);
      } else {
        toast.error(result.error ?? `${PROVIDER_LABELS[provider]} test failed`);
      }
    } catch (err) {
      const msg = errorMessage(err, 'Connection failed');
      setTestResults((prev) => ({ ...prev, [provider]: { ok: false, error: msg } }));
      toast.error(msg);
    } finally {
      setTesting((prev) => ({ ...prev, [provider]: false }));
    }
  };

  if (isLoading) {
    return <div className="py-4 text-sm text-muted-foreground">Loading provider settings…</div>;
  }

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardHeader>
        <p className="text-sm text-muted-foreground">
          Configure cloud LLM keys to route the <code className="text-xs">smart</code>/
          <code className="text-xs">fast</code> aliases to Claude, GPT, or Gemini. Keys are
          encrypted at rest. Leave all blank to keep using local Ollama models.
        </p>
      </CardHeader>
      <CardContent className="space-y-6">
        {PROVIDERS.map((provider) => {
          const maskedValue = getMaskedKey(configs, provider);
          const draft = drafts[provider];
          const isTesting = testing[provider] ?? false;
          const result = testResults[provider] ?? null;
          const status = providerStatus(maskedValue, result);
          const StatusIcon = status.icon;

          return (
            <div key={provider} className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Label htmlFor={`provider-key-${provider}`}>{PROVIDER_LABELS[provider]}</Label>
                <span className={`flex items-center gap-1 text-xs ${status.className}`}>
                  <StatusIcon className="h-3 w-3" />
                  {status.label}
                </span>
              </div>
              <div className="flex gap-2">
                <Input
                  id={`provider-key-${provider}`}
                  type="password"
                  placeholder={PROVIDER_PLACEHOLDERS[provider]}
                  value={draft ?? maskedValue}
                  onChange={(e) => handleChange(provider, e.target.value)}
                  onBlur={() => handleBlur(provider)}
                  autoComplete="off"
                  className="flex-1"
                />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handleTest(provider)}
                  disabled={isTesting}
                  aria-label={`Test ${PROVIDER_LABELS[provider]} connection`}
                >
                  {isTesting ? (
                    <Loader2 className="h-3 w-3 mr-1 animate-spin" />
                  ) : null}
                  {isTesting ? 'Testing…' : 'Test'}
                </Button>
              </div>
              {result !== null && result.ok && (
                <span
                  className="flex items-center gap-1 text-xs text-[var(--status-ok)]"
                >
                  <CheckCircle className="h-3 w-3" />
                  Connected
                </span>
              )}
              <p className="text-xs text-muted-foreground">
                Stored encrypted at rest. Leave blank to use local Ollama models.
              </p>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
