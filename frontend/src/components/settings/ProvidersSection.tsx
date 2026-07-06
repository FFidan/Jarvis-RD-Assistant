import { useMemo, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { compareCloudProviders, fetchConfig, listProviders, setConfig, testProvider } from '@/lib/api';
import type { ProviderMetadata } from '@/lib/api';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Loader2, CheckCircle, CircleDashed, ShieldAlert, Plus, KeyRound } from 'lucide-react';
import { toast } from 'sonner';
import { errorMessage } from '@/lib/errors';
import type { ConfigEntry } from '@/types';

type TestState = { ok: boolean; error: string | null } | null;

type DraftState = {
  apiKey?: string | null;
  baseUrl?: string | null;
};

function getMaskedConfig(configs: ConfigEntry[], key: string | null | undefined): string {
  if (!key) return '';
  const entry = configs.find((c) => c.key === key);
  if (entry == null) return '';
  const v = entry.value;
  if (typeof v === 'string') return v.replace(/^"|"$/g, '');
  return '';
}

function providerStatus(provider: ProviderMetadata, maskedValue: string, result: TestState) {
  if (result?.ok) {
    return {
      icon: CheckCircle,
      label: maskedValue || provider.configured ? 'Configured and tested' : 'Tested',
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
  if (!maskedValue && !provider.configured) {
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

function providerGroupLabel(provider: ProviderMetadata): string {
  if (provider.kind === 'router') return 'Recommended routers';
  if (provider.kind === 'self_hosted') return 'Advanced endpoints';
  return 'Direct providers';
}

function sortProviders(providers: ProviderMetadata[]): ProviderMetadata[] {
  return [...providers].sort((a, b) => compareCloudProviders(a.id, b.id) || a.display_name.localeCompare(b.display_name));
}

export function ProvidersSection() {
  const queryClient = useQueryClient();

  const {
    data: configs = [],
    isLoading: configsLoading,
    isError: configsError,
    error: configsErrorValue,
  } = useQuery({
    queryKey: QUERY_KEYS.config.all(),
    queryFn: fetchConfig,
  });
  const {
    data: providerRows = [],
    isLoading: providersLoading,
    isError: providersError,
    error: providersErrorValue,
  } = useQuery({
    queryKey: ['settings', 'providers'],
    queryFn: listProviders,
  });

  const loadError = configsError
    ? errorMessage(configsErrorValue, 'Could not load stored provider keys')
    : providersError
      ? errorMessage(providersErrorValue, 'Could not load provider metadata')
      : null;
  const providers = useMemo(() => sortProviders(providerRows), [providerRows]);
  const configuredProviders = providers.filter(
    (provider) => provider.configured || getMaskedConfig(configs, provider.api_key_config_key),
  );
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(null);
  const [chooserOpen, setChooserOpen] = useState(false);
  const selectedProvider =
    providers.find((provider) => provider.id === selectedProviderId) ?? configuredProviders[0] ?? providers[0];

  const [drafts, setDrafts] = useState<Record<string, DraftState>>({});
  const [testing, setTesting] = useState<Record<string, boolean>>({});
  const [testResults, setTestResults] = useState<Record<string, TestState>>({});

  const saveMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: string }) => setConfig(key, value),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() });
      queryClient.invalidateQueries({ queryKey: ['settings', 'providers'] });
    },
    onError: (err: Error) => {
      toast.error(err.message ?? 'Failed to save provider setting');
    },
  });

  const handleDraft = (providerId: string, field: keyof DraftState, value: string) => {
    setDrafts((prev) => ({ ...prev, [providerId]: { ...prev[providerId], [field]: value } }));
  };

  const saveIfChanged = (provider: ProviderMetadata, field: keyof DraftState) => {
    const key = field === 'apiKey' ? provider.api_key_config_key : provider.base_url_config_key;
    if (!key) return;
    const draft = drafts[provider.id]?.[field];
    const current = getMaskedConfig(configs, key);
    if (draft !== null && draft !== undefined && draft.trim() !== '' && draft !== current) {
      saveMut.mutate({ key, value: draft.trim() });
    }
    setDrafts((prev) => ({ ...prev, [provider.id]: { ...prev[provider.id], [field]: null } }));
  };

  const handleTest = async (provider: ProviderMetadata) => {
    setTesting((prev) => ({ ...prev, [provider.id]: true }));
    setTestResults((prev) => ({ ...prev, [provider.id]: null }));
    try {
      const result = await testProvider(provider.id);
      setTestResults((prev) => ({ ...prev, [provider.id]: result }));
      if (result.ok) {
        toast.success(`${provider.display_name} connection OK`);
      } else {
        toast.error(result.error ?? `${provider.display_name} test failed`);
      }
    } catch (err) {
      const msg = errorMessage(err, 'Connection failed');
      setTestResults((prev) => ({ ...prev, [provider.id]: { ok: false, error: msg } }));
      toast.error(msg);
    } finally {
      setTesting((prev) => ({ ...prev, [provider.id]: false }));
    }
  };

  if (configsLoading || providersLoading) {
    return <div className="py-4 text-sm text-muted-foreground">Loading provider settings...</div>;
  }

  if (loadError) {
    return (
      <Card className="rounded-md border-hair shadow-none">
        <CardHeader className="space-y-2">
          <h3 className="text-base font-semibold">Providers &amp; Routing</h3>
          <p className="max-w-3xl text-sm text-muted-foreground">
            Local models stay the default. Provider keys are deployment-wide and encrypted at rest.
          </p>
        </CardHeader>
        <CardContent>
          <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
            Could not load provider settings. {loadError}
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardHeader className="space-y-2">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1">
            <h3 className="text-base font-semibold">Providers &amp; Routing</h3>
            <p className="max-w-3xl text-sm text-muted-foreground">
              Local models stay the default. Add cloud providers only when you want selected Main or
              Quick model routes to use external compute. Provider keys are deployment-wide and
              encrypted at rest.
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={() => setChooserOpen((open) => !open)}>
            <Plus className="mr-2 h-4 w-4" />
            Add cloud provider
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-5">
        {configuredProviders.length > 0 && (
          <div className="space-y-2">
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Connected
            </p>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
              {configuredProviders.map((provider) => {
                const maskedValue = getMaskedConfig(configs, provider.api_key_config_key);
                const status = providerStatus(provider, maskedValue, testResults[provider.id] ?? null);
                const StatusIcon = status.icon;
                return (
                  <button
                    key={provider.id}
                    type="button"
                    onClick={() => setSelectedProviderId(provider.id)}
                    className="flex min-h-16 items-center justify-between gap-3 rounded-md border border-hair bg-background px-3 py-2 text-left hover:border-primary focus:outline-none focus:ring-2 focus:ring-primary"
                  >
                    <span>
                      <span className="block text-sm font-medium">{provider.display_name}</span>
                      <span className={`mt-1 flex items-center gap-1 text-xs ${status.className}`}>
                        <StatusIcon className="h-3 w-3" />
                        {status.label}
                      </span>
                    </span>
                    <KeyRound className="h-4 w-4 text-muted-foreground" />
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {chooserOpen && (
          <div className="space-y-3 rounded-md border border-hair bg-muted/20 p-3">
            {['Direct providers', 'Recommended routers', 'Advanced endpoints'].map((group) => {
              const groupProviders = providers.filter((provider) => providerGroupLabel(provider) === group);
              if (groupProviders.length === 0) return null;
              return (
                <div key={group} className="space-y-2">
                  <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    {group}
                  </p>
                  <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                    {groupProviders.map((provider) => (
                      <Button
                        key={provider.id}
                        type="button"
                        variant="outline"
                        className="h-auto justify-start whitespace-normal py-2 text-left"
                        onClick={() => {
                          setSelectedProviderId(provider.id);
                          setChooserOpen(false);
                        }}
                      >
                        <span>
                          <span className="block text-sm font-medium">{provider.display_name}</span>
                          <span className="block text-xs text-muted-foreground">{provider.best_for}</span>
                        </span>
                      </Button>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {selectedProvider && (
          <div className="space-y-4 rounded-md border border-hair p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h4 className="text-sm font-semibold">{selectedProvider.display_name}</h4>
                <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
                  {selectedProvider.best_for}
                </p>
              </div>
              <span className="rounded-sm border border-hair px-2 py-1 text-xs text-muted-foreground">
                {selectedProvider.privacy_boundary.replace(/_/g, ' ')}
              </span>
            </div>

            <div className="grid gap-3 lg:grid-cols-[1fr_auto]">
              <div className="space-y-2">
                <Label htmlFor={`provider-key-${selectedProvider.id}`}>API key</Label>
                <Input
                  id={`provider-key-${selectedProvider.id}`}
                  type="password"
                  placeholder="Paste provider API key"
                  value={
                    drafts[selectedProvider.id]?.apiKey ??
                    getMaskedConfig(configs, selectedProvider.api_key_config_key)
                  }
                  onChange={(e) => handleDraft(selectedProvider.id, 'apiKey', e.target.value)}
                  onBlur={() => saveIfChanged(selectedProvider, 'apiKey')}
                  autoComplete="off"
                />
              </div>
              <div className="flex items-end">
                <Button
                  variant="outline"
                  onClick={() => handleTest(selectedProvider)}
                  disabled={testing[selectedProvider.id] ?? false}
                  aria-label={`Test ${selectedProvider.display_name} connection`}
                >
                  {testing[selectedProvider.id] ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : null}
                  {testing[selectedProvider.id] ? 'Testing...' : 'Test'}
                </Button>
              </div>
            </div>

            {selectedProvider.base_url_config_key && (
              <div className="space-y-2">
                <Label htmlFor={`provider-base-url-${selectedProvider.id}`}>Base URL</Label>
                <Input
                  id={`provider-base-url-${selectedProvider.id}`}
                  type="url"
                  placeholder="https://example.com/v1"
                  value={
                    drafts[selectedProvider.id]?.baseUrl ??
                    getMaskedConfig(configs, selectedProvider.base_url_config_key)
                  }
                  onChange={(e) => handleDraft(selectedProvider.id, 'baseUrl', e.target.value)}
                  onBlur={() => saveIfChanged(selectedProvider, 'baseUrl')}
                  autoComplete="off"
                />
              </div>
            )}

            <p className="text-xs text-muted-foreground">
              Admin-wide setting. {selectedProvider.data_note} Leave all provider keys blank to keep this
              deployment local-only; local Ollama/vLLM routes remain available.
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
