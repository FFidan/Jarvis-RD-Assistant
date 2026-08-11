import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { formatDistanceToNow } from 'date-fns';
import { CheckCircle, CircleDashed, ExternalLink, KeyRound, Loader2, Plus, ShieldAlert } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  compareCloudProviders,
  fetchConfig,
  fetchProviderAccount,
  fetchSystemModels,
  listProviders,
  setConfig,
  testProvider,
} from '@/lib/api';
import type { ProviderMetadata, ProviderModelListStatus } from '@/lib/api';
import { errorMessage } from '@/lib/errors';
import { QUERY_KEYS } from '@/lib/query-keys';
import type { ConfigEntry } from '@/types';

type EditableField = 'apiKey' | 'baseUrl';
type DraftState = Partial<Record<EditableField, string>>;
type TestState = { ok: boolean; error: string | null } | null;

interface SaveProviderConfigVariables {
  providerId: string;
  field: EditableField;
  key: string;
  value: string;
}

function getMaskedConfig(configs: ConfigEntry[], key: string | null | undefined): string {
  if (!key) return '';
  const value = configs.find((entry) => entry.key === key)?.value;
  return typeof value === 'string' ? value.replace(/^"|"$/g, '') : '';
}

function providerGroupLabel(provider: ProviderMetadata): string {
  if (provider.kind === 'router') return 'Recommended routers';
  if (provider.kind === 'self_hosted') return 'Advanced endpoints';
  return 'Direct providers';
}

function sortProviders(providers: ProviderMetadata[]): ProviderMetadata[] {
  return [...providers].sort(
    (left, right) =>
      compareCloudProviders(left.id, right.id) || left.display_name.localeCompare(right.display_name),
  );
}

function providerAvailabilityText(
  catalogCount: number,
  status: ProviderModelListStatus | undefined,
): string | null {
  if (!status) return null;
  if (status.error) return "Catalog unavailable — JARVIS could not refresh this provider's models";
  if (catalogCount === 0) return 'Catalog checked — no compatible models were returned';
  const checked = status.fetched_at
    ? formatDistanceToNow(new Date(status.fetched_at), { addSuffix: true })
    : null;
  const count = `${catalogCount} model${catalogCount === 1 ? '' : 's'} available`;
  return checked ? `${count} · Checked ${checked}` : count;
}

function providerStatus(
  provider: ProviderMetadata,
  status: ProviderModelListStatus | undefined,
) {
  if (!provider.configured && !provider.base_url_configured) {
    return { icon: CircleDashed, label: 'Not configured', className: 'text-muted-foreground' };
  }
  if (status?.error) {
    return { icon: ShieldAlert, label: 'Configured; connection needs attention', className: 'text-destructive' };
  }
  if (status?.fetched_at) {
    const checked = formatDistanceToNow(new Date(status.fetched_at), { addSuffix: true });
    return { icon: CheckCircle, label: `Connected · checked ${checked}`, className: 'text-[var(--status-ok)]' };
  }
  return { icon: CircleDashed, label: 'Configured; not checked yet', className: 'text-[var(--status-warn)]' };
}

function accountStatusText(errorCode: string | null | undefined): string {
  if (!errorCode) return 'Temporarily unavailable';
  return {
    provider_authentication_failed: 'Provider rejected this key',
    provider_payment_required: 'Provider account needs billing attention',
    provider_rate_limited: 'Provider rate limit reached',
    provider_unavailable: 'Provider service is unavailable',
    provider_request_timed_out: 'Provider did not respond in time',
    egress_blocked: 'Outbound provider access is disabled',
    api_key_unavailable: 'Provider key is unavailable',
    provider_response_too_large: 'Provider returned too much account data',
    provider_response_invalid: 'Provider returned unsupported account data',
    provider_request_failed: 'Provider account request failed',
    provider_http_error: 'Provider rejected the account request',
  }[errorCode] ?? 'Provider account data is unavailable';
}

function accountLabel(key: string): string {
  const knownLabels: Record<string, string> = {
    is_free_tier: 'Free tier',
    usage: 'Usage',
    usage_daily: 'Usage today',
    usage_weekly: 'Usage this week',
    usage_monthly: 'Usage this month',
    limit: 'Limit',
    limit_remaining: 'Limit remaining',
    limit_reset: 'Limit resets',
    expires_at: 'Expires',
    available_balance: 'Available balance',
    voucher_balance: 'Voucher balance',
    cash_balance: 'Cash balance',
  };
  const known = knownLabels[key];
  if (known) return known;
  const currencyMatch = key.match(/^(total|granted|topped_up)_balance_([a-z]{3})$/);
  if (currencyMatch) {
    const balanceKind = currencyMatch[1] ?? '';
    const currency = currencyMatch[2] ?? '';
    const kind = balanceKind === 'topped_up'
      ? 'Topped-up'
      : `${balanceKind.charAt(0).toUpperCase()}${balanceKind.slice(1)}`;
    return `${kind} balance (${currency.toUpperCase()})`;
  }
  return key.replace(/_/g, ' ');
}

function accountValue(value: boolean | number | string | null): string {
  if (value == null) return 'Unavailable';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number') return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return value;
}

function AccountSnapshot({
  provider,
  connection,
  models,
}: {
  provider: ProviderMetadata;
  connection: string;
  models: string;
}) {
  const accountQuery = useQuery({
    queryKey: QUERY_KEYS.config.providerAccount(provider.id),
    queryFn: () => fetchProviderAccount(provider.id),
    enabled: provider.account_capability !== 'unavailable' && provider.configured,
    staleTime: 60_000,
  });
  const entries = Object.entries(accountQuery.data?.data ?? {});
  const accountStatus = (() => {
    if (provider.account_capability === 'unavailable') {
      return 'Not exposed by this provider API';
    }
    if (!provider.configured) return 'Add a key to check';
    if (accountQuery.isLoading) return 'Loading';
    if (accountQuery.isError) return 'Provider account request failed';
    if (accountQuery.data?.error_code) return accountStatusText(accountQuery.data.error_code);
    return entries.length > 0 ? 'Available' : 'No supported fields returned';
  })();

  return (
    <AccountPanel title="Provider account">
      <dl className="divide-y divide-hair text-sm">
        <div className="flex items-start justify-between gap-4 py-2">
          <dt className="text-muted-foreground">Connection</dt>
          <dd className="text-right">{connection}</dd>
        </div>
        <div className="flex items-start justify-between gap-4 py-2">
          <dt className="text-muted-foreground">Models</dt>
          <dd className="text-right">{models}</dd>
        </div>
        <div className="flex items-start justify-between gap-4 py-2">
          <dt className="text-muted-foreground">Account data</dt>
          <dd className="text-right">{accountStatus}</dd>
        </div>
        <div className="flex items-start justify-between gap-4 py-2">
          <dt className="text-muted-foreground">Provider dashboard</dt>
          <dd className="text-right">
            <ProviderDashboardLink provider={provider} />
          </dd>
        </div>
      </dl>
      {entries.length > 0 && !accountQuery.isError && !accountQuery.data?.error_code && (
        <div className="space-y-2 border-t border-hair pt-3">
          <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
            {provider.account_capability === 'balance' ? 'Provider-reported balances' : 'Current-key details'}
          </p>
          <dl className="divide-y divide-hair text-sm">
          {entries.map(([key, value]) => (
            <div key={key} className="flex items-center justify-between gap-4 py-2">
              <dt className="text-muted-foreground">{accountLabel(key)}</dt>
              <dd className="font-mono text-right">{accountValue(value)}</dd>
            </div>
          ))}
          </dl>
        </div>
      )}
    </AccountPanel>
  );
}

function AccountPanel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <aside className="space-y-3 rounded-md border border-hair p-4" aria-label="Provider account snapshot">
      <div>
        <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">Account snapshot</p>
        <h4 className="mt-1 text-sm font-semibold">{title}</h4>
      </div>
      {children}
    </aside>
  );
}

function ProviderDashboardLink({ provider }: { provider: ProviderMetadata }) {
  if (!provider.dashboard_url) {
    return <span className="text-xs text-muted-foreground">Unavailable</span>;
  }
  return (
    <a
      href={provider.dashboard_url}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 text-xs text-primary underline-offset-4 hover:underline"
    >
      Open provider dashboard
      <ExternalLink className="h-3 w-3" />
    </a>
  );
}

export function ProvidersSection({ initialProviderId }: { initialProviderId?: string } = {}) {
  const queryClient = useQueryClient();
  const configQuery = useQuery({ queryKey: QUERY_KEYS.config.all(), queryFn: fetchConfig });
  const providersQuery = useQuery({ queryKey: QUERY_KEYS.config.providers(), queryFn: listProviders });
  const modelsQuery = useQuery({
    queryKey: QUERY_KEYS.config.systemModels(),
    queryFn: ({ signal }) => fetchSystemModels(signal),
    staleTime: 60_000,
  });
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(
    initialProviderId ?? null,
  );
  const [chooserOpen, setChooserOpen] = useState(false);
  const [editing, setEditing] = useState<Record<string, Partial<Record<EditableField, boolean>>>>({});
  const [drafts, setDrafts] = useState<Record<string, DraftState>>({});
  const [testResults, setTestResults] = useState<Record<string, TestState>>({});

  const providers = useMemo(() => sortProviders(providersQuery.data ?? []), [providersQuery.data]);
  const configured = providers.filter((provider) => provider.configured || provider.base_url_configured);
  const selected =
    providers.find((provider) => provider.id === selectedProviderId) ?? configured[0] ?? providers[0];
  const configs = configQuery.data ?? [];
  const providerLists = modelsQuery.data?.provider_lists ?? {};
  const catalog = modelsQuery.data?.catalog ?? [];

  const saveMutation = useMutation({
    mutationFn: ({ key, value }: SaveProviderConfigVariables) => setConfig(key, value),
    onSuccess: (_result, variables) => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.providers() });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.systemModels() });
      setEditing((previous) => ({
        ...previous,
        [variables.providerId]: { ...previous[variables.providerId], [variables.field]: false },
      }));
      setDrafts((previous) => ({
        ...previous,
        [variables.providerId]: { ...previous[variables.providerId], [variables.field]: undefined },
      }));
      toast.success(variables.field === 'apiKey' ? 'Provider key saved' : 'Provider endpoint saved');
    },
    onError: (error: Error) => toast.error(error.message || 'Failed to save provider setting'),
  });

  const beginEdit = (provider: ProviderMetadata, field: EditableField) => {
    setDrafts((previous) => ({ ...previous, [provider.id]: { ...previous[provider.id], [field]: '' } }));
    setEditing((previous) => ({ ...previous, [provider.id]: { ...previous[provider.id], [field]: true } }));
  };
  const cancelEdit = (provider: ProviderMetadata, field: EditableField) => {
    setDrafts((previous) => ({ ...previous, [provider.id]: { ...previous[provider.id], [field]: undefined } }));
    setEditing((previous) => ({ ...previous, [provider.id]: { ...previous[provider.id], [field]: false } }));
  };
  const saveField = (provider: ProviderMetadata, field: EditableField) => {
    const key = field === 'apiKey' ? provider.api_key_config_key : provider.base_url_config_key;
    const value = drafts[provider.id]?.[field]?.trim();
    if (!key || !value) return;
    saveMutation.mutate({ providerId: provider.id, field, key, value });
  };

  const testMutation = useMutation({
    mutationFn: (provider: ProviderMetadata) => testProvider(provider.id),
    onSuccess: (result, provider) => {
      setTestResults((previous) => ({ ...previous, [provider.id]: result }));
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.systemModels() });
      if (result.ok) {
        toast.success(`${provider.display_name} connection passed`);
      } else {
        toast.error(result.error ?? `${provider.display_name} connection failed`);
      }
    },
    onError: (error: Error, provider) => {
      const message = errorMessage(error, 'Connection failed');
      setTestResults((previous) => ({ ...previous, [provider.id]: { ok: false, error: message } }));
      toast.error(message);
    },
  });

  if (configQuery.isLoading || providersQuery.isLoading) {
    return <div className="py-4 text-sm text-muted-foreground">Loading provider settings...</div>;
  }
  if (configQuery.isError || providersQuery.isError) {
    const loadError = configQuery.isError
      ? errorMessage(configQuery.error, 'Could not load stored provider keys')
      : errorMessage(providersQuery.error, 'Could not load provider metadata');
    return (
      <Card className="rounded-md border-hair shadow-none">
        <CardContent className="p-4">
          <p role="alert" className="text-sm text-destructive">Could not load provider settings. {loadError}</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardHeader className="space-y-2">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold">Providers &amp; Routing</h3>
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
              Add cloud access only when a Quick or Main route should use external compute. Provider keys are deployment-wide and encrypted at rest.
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={() => setChooserOpen((open) => !open)}>
            <Plus className="mr-2 h-4 w-4" />
            Add cloud provider
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-5">
        {configured.length > 0 && (
          <section className="space-y-2" aria-labelledby="connected-providers-heading">
            <p id="connected-providers-heading" className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">Connected providers</p>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              {configured.map((provider) => {
                const listStatus = providerLists[provider.id];
                const status = providerStatus(provider, listStatus);
                const StatusIcon = status.icon;
                const count = catalog.filter((entry) => entry.provider === provider.id).length;
                return (
                  <button
                    key={provider.id}
                    type="button"
                    onClick={() => setSelectedProviderId(provider.id)}
                    aria-pressed={selected?.id === provider.id}
                    className="min-h-24 rounded-md border border-hair bg-background px-3 py-2 text-left hover:border-primary aria-pressed:border-primary aria-pressed:bg-muted/40 focus:outline-none focus:ring-2 focus:ring-primary"
                  >
                    <span className="block text-sm font-medium">{provider.display_name}</span>
                    <span className={`mt-1 flex items-center gap-1 text-xs ${status.className}`}>
                      <StatusIcon className="h-3 w-3" />{status.label}
                    </span>
                    {providerAvailabilityText(count, listStatus) && (
                      <span className="mt-1 block text-xs text-muted-foreground">{providerAvailabilityText(count, listStatus)}</span>
                    )}
                  </button>
                );
              })}
            </div>
          </section>
        )}

        {chooserOpen && (
          <div className="space-y-3 rounded-md border border-hair bg-muted/20 p-3">
            {['Direct providers', 'Recommended routers', 'Advanced endpoints'].map((group) => {
              const rows = providers.filter((provider) => providerGroupLabel(provider) === group);
              if (rows.length === 0) return null;
              return (
                <section key={group} className="space-y-2">
                  <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">{group}</p>
                  <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                    {rows.map((provider) => (
                      <Button
                        key={provider.id}
                        type="button"
                        variant="outline"
                        className="h-auto justify-start whitespace-normal py-2 text-left"
                        onClick={() => { setSelectedProviderId(provider.id); setChooserOpen(false); }}
                      >
                        <span><span className="block font-medium">{provider.display_name}</span><span className="block text-xs text-muted-foreground">{provider.best_for}</span></span>
                      </Button>
                    ))}
                  </div>
                </section>
              );
            })}
          </div>
        )}

        {selected && (
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
            <section className="space-y-4 rounded-md border border-hair p-4" aria-label={`${selected.display_name} credentials and privacy`}>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div><p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">Credential and privacy</p><h4 className="mt-1 text-base font-semibold">{selected.display_name}</h4><p className="mt-1 text-sm text-muted-foreground">{selected.best_for}</p></div>
                <span className="text-xs text-muted-foreground">{selected.privacy_boundary.replace(/_/g, ' ')}</span>
              </div>

              <div className="space-y-2">
                <Label htmlFor={`provider-key-${selected.id}`}>API key</Label>
                {editing[selected.id]?.apiKey ? (
                  <div className="flex flex-col gap-2 sm:flex-row">
                    <Input id={`provider-key-${selected.id}`} type="password" autoComplete="off" placeholder="Paste a new provider API key" value={drafts[selected.id]?.apiKey ?? ''} onChange={(event) => setDrafts((previous) => ({ ...previous, [selected.id]: { ...previous[selected.id], apiKey: event.target.value } }))} />
                    <Button onClick={() => saveField(selected, 'apiKey')} disabled={!drafts[selected.id]?.apiKey?.trim() || saveMutation.isPending}>Save key</Button>
                    <Button variant="outline" onClick={() => cancelEdit(selected, 'apiKey')}>Cancel</Button>
                  </div>
                ) : (
                  <div className="flex flex-col gap-2 sm:flex-row">
                    <Input id={`provider-key-${selected.id}`} readOnly value={getMaskedConfig(configs, selected.api_key_config_key) || (selected.configured ? 'Configured' : 'Not configured')} aria-label={`${selected.display_name} stored key status`} />
                    <Button variant="outline" onClick={() => beginEdit(selected, 'apiKey')}><KeyRound className="mr-2 h-4 w-4" />{selected.configured ? 'Replace key' : 'Add key'}</Button>
                    <Button variant="outline" onClick={() => testMutation.mutate(selected)} disabled={!selected.configured || testMutation.isPending}>{testMutation.isPending && testMutation.variables?.id === selected.id ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}Test now</Button>
                  </div>
                )}
              </div>

              {selected.base_url_config_key && (
                <div className="space-y-2">
                  <Label htmlFor={`provider-base-url-${selected.id}`}>Base URL</Label>
                  {editing[selected.id]?.baseUrl ? (
                    <div className="flex flex-col gap-2 sm:flex-row">
                      <Input id={`provider-base-url-${selected.id}`} type="url" placeholder="https://example.com/v1" value={drafts[selected.id]?.baseUrl ?? ''} onChange={(event) => setDrafts((previous) => ({ ...previous, [selected.id]: { ...previous[selected.id], baseUrl: event.target.value } }))} />
                      <Button onClick={() => saveField(selected, 'baseUrl')} disabled={!drafts[selected.id]?.baseUrl?.trim() || saveMutation.isPending}>Save endpoint</Button>
                      <Button variant="outline" onClick={() => cancelEdit(selected, 'baseUrl')}>Cancel</Button>
                    </div>
                  ) : (
                    <div className="flex flex-col gap-2 sm:flex-row">
                      <Input id={`provider-base-url-${selected.id}`} readOnly value={getMaskedConfig(configs, selected.base_url_config_key) || (selected.base_url_configured ? 'Configured' : 'Not configured')} />
                      <Button variant="outline" onClick={() => beginEdit(selected, 'baseUrl')}>{selected.base_url_configured ? 'Replace endpoint' : 'Add endpoint'}</Button>
                    </div>
                  )}
                </div>
              )}

              <div className="rounded-md border border-primary/25 bg-primary/5 p-3 text-xs text-muted-foreground">
                {selected.data_note} Leave every provider key blank to keep this deployment local-only.
              </div>
              {testResults[selected.id]?.ok === false && <p role="alert" className="text-xs text-destructive">Connection test failed: {testResults[selected.id]?.error ?? 'Unknown provider error'}</p>}
              {selected.supports_assignment && (
                <div className="flex flex-wrap gap-3 text-sm">
                  <Link className="text-primary underline-offset-4 hover:underline" to={`/settings?section=models&item=llm&role=fast&provider=${encodeURIComponent(selected.id)}`}>
                    Use for Quick
                  </Link>
                  <Link className="text-primary underline-offset-4 hover:underline" to={`/settings?section=models&item=llm&role=smart&provider=${encodeURIComponent(selected.id)}`}>
                    Use for Main
                  </Link>
                </div>
              )}
            </section>
            <AccountSnapshot
              provider={selected}
              connection={providerStatus(
                selected,
                providerLists[selected.id],
              ).label}
              models={
                providerLists[selected.id]?.error
                  ? 'Catalog unavailable'
                  : providerLists[selected.id]
                    ? `${catalog.filter((entry) => entry.provider === selected.id).length} available`
                    : 'Not checked'
              }
            />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
