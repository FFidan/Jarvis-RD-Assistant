import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchConfig, setConfig, zoteroTest, zoteroPollNow } from '@/lib/api';
import { useJobStore } from '@/stores/job-store';
import { onSaveError } from '@/lib/forms/save-error';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { ScheduleSelect } from '@/components/ui/schedule-select';
import { CheckCircle, XCircle, Loader2, ChevronDown, ChevronRight } from 'lucide-react';
import { QueryErrorState } from '@/components/shared/QueryErrorState';
import type { ConfigEntry } from '@/types';
import { toast } from 'sonner';

const ZOTERO_LIBRARY_SCOPE_KEYS = new Set([
  'zotero.library_type',
  'zotero.user_id',
  'zotero.group_id',
]);

function getAllowedPrivateHosts(configs: ConfigEntry[]): string[] {
  const entry = configs.find((c) => c.key === 'zotero.allowed_private_hosts');
  if (entry == null || !Array.isArray(entry.value)) return [];
  return entry.value.filter((h): h is string => typeof h === 'string');
}

function getConfigValue(configs: ConfigEntry[], key: string): string {
  const entry = configs.find((c) => c.key === key);
  if (entry == null) return '';
  const v = entry.value;
  if (typeof v === 'string') return v.replace(/^"|"$/g, '');
  if (typeof v === 'boolean') return String(v);
  return String(v ?? '');
}

export function ZoteroSection() {
  const queryClient = useQueryClient();

  const { data: configs = [], isLoading, isError } = useQuery({
    queryKey: QUERY_KEYS.config.all(),
    queryFn: fetchConfig,
  });

  // Derived values from config
  const apiKey = getConfigValue(configs, 'zotero.api_key');
  const userId = getConfigValue(configs, 'zotero.user_id');
  const libraryType = getConfigValue(configs, 'zotero.library_type') || 'user';
  const groupIdRaw = getConfigValue(configs, 'zotero.group_id');
  const autoPush = getConfigValue(configs, 'zotero.auto_push_on_star') === 'true';
  const pollEnabled = getConfigValue(configs, 'zotero.poll_enabled') === 'true';
  const pollCron = getConfigValue(configs, 'zotero.poll_cron') || '';
  const allowedPrivateHosts = getAllowedPrivateHosts(configs).join(', ');

  // Local draft state for text inputs the user types. Committed together by
  // the section's explicit Save button, not per field on blur.
  const [draftApiKey, setDraftApiKey] = useState<string | null>(null);
  const [draftUserId, setDraftUserId] = useState<string | null>(null);
  const [draftGroupId, setDraftGroupId] = useState<string | null>(null);
  const [draftPollCron, setDraftPollCron] = useState<string | null>(null);
  const [draftAllowedHosts, setDraftAllowedHosts] = useState<string | null>(null);

  // Test connection state
  const [testResult, setTestResult] = useState<{ success: boolean; error?: string } | null>(null);
  const [isTesting, setIsTesting] = useState(false);

  // Sync now state
  const [isSyncing, setIsSyncing] = useState(false);
  const [libraryScopeChanged, setLibraryScopeChanged] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const setMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: (_data, variables) => {
      if (ZOTERO_LIBRARY_SCOPE_KEYS.has(variables.key)) {
        setLibraryScopeChanged(true);
      }
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() });
    },
    onError: onSaveError('Failed to save Zotero setting.'),
  });

  // Whether any typed field has an unsaved edit, for the Save button's enabled state.
  const hasDraftChanges =
    (draftApiKey !== null && draftApiKey !== apiKey) ||
    (draftUserId !== null && draftUserId !== userId) ||
    (draftGroupId !== null && draftGroupId !== groupIdRaw) ||
    (draftPollCron !== null && draftPollCron !== pollCron) ||
    (draftAllowedHosts !== null && draftAllowedHosts !== allowedPrivateHosts);

  const handleSave = () => {
    if (draftApiKey !== null && draftApiKey !== apiKey) {
      setMut.mutate({ key: 'zotero.api_key', value: draftApiKey });
    }
    if (draftUserId !== null && draftUserId !== userId) {
      setMut.mutate({ key: 'zotero.user_id', value: draftUserId });
    }
    if (draftGroupId !== null && draftGroupId !== groupIdRaw) {
      const parsed = draftGroupId === '' ? null : Number.parseInt(draftGroupId, 10);
      if (draftGroupId === '' || (parsed !== null && Number.isInteger(parsed) && parsed > 0)) {
        setMut.mutate({ key: 'zotero.group_id', value: parsed });
      }
    }
    if (draftPollCron !== null && draftPollCron !== pollCron) {
      setMut.mutate({ key: 'zotero.poll_cron', value: draftPollCron });
    }
    if (draftAllowedHosts !== null && draftAllowedHosts !== allowedPrivateHosts) {
      const hosts = draftAllowedHosts
        .split(',')
        .map((h) => h.trim())
        .filter((h) => h !== '');
      setMut.mutate({ key: 'zotero.allowed_private_hosts', value: hosts });
    }
    setDraftApiKey(null);
    setDraftUserId(null);
    setDraftGroupId(null);
    setDraftPollCron(null);
    setDraftAllowedHosts(null);
  };

  const handleLibraryTypeChange = (type: 'user' | 'group') => {
    setMut.mutate({ key: 'zotero.library_type', value: type });
  };

  const handleAutoPushChange = (checked: boolean) => {
    setMut.mutate({ key: 'zotero.auto_push_on_star', value: checked });
  };

  const handlePollEnabledChange = (checked: boolean) => {
    setMut.mutate({ key: 'zotero.poll_enabled', value: checked });
  };

  const handleTestConnection = async () => {
    setIsTesting(true);
    setTestResult(null);
    try {
      const result = await zoteroTest();
      setTestResult(result);
    } catch {
      setTestResult({ success: false, error: 'Connection failed' });
    } finally {
      setIsTesting(false);
    }
  };

  const handleSyncNow = async () => {
    setIsSyncing(true);
    try {
      const response = await zoteroPollNow();
      const status:
        | 'queued'
        | 'running'
        | 'succeeded'
        | 'failed'
        | 'cancelled' =
        response.status === 'running' ||
        response.status === 'succeeded' ||
        response.status === 'failed' ||
        response.status === 'cancelled'
          ? response.status
          : 'queued';
      useJobStore.getState().trackExternalJob({
        jobId: response.job_id,
        kind: 'zotero.poll',
        payload: {},
        status,
      });
      setLibraryScopeChanged(false);
      toast.success('Zotero library sync queued.');
    } catch {
      toast.error('Zotero sync failed to queue.');
    } finally {
      setIsSyncing(false);
    }
  };

  if (isLoading) {
    return <div className="py-4 text-sm text-muted-foreground">Loading Zotero settings…</div>;
  }

  if (isError) {
    // A blank form would read as "Zotero not configured" — show the failure instead.
    return <QueryErrorState message="Failed to load Zotero settings." />;
  }

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardHeader>
        <p className="text-sm text-muted-foreground">
          Connect JARVIS to your Zotero library to push papers and copy citation keys. Linked
          projects determine the Zotero collection, and citation metadata is sent before
          annotations or highlights are synchronized.
        </p>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* API Key */}
        <div className="space-y-2">
          <Label htmlFor="zotero-api-key">API Key</Label>
          <Input
            id="zotero-api-key"
            type="password"
            placeholder="Enter your Zotero API key"
            value={draftApiKey ?? apiKey}
            onChange={(e) => setDraftApiKey(e.target.value)}
            autoComplete="off"
          />
          <p className="text-xs text-muted-foreground">
            Generate a key at{' '}
            <a
              href="https://www.zotero.org/settings/keys"
              target="_blank"
              rel="noopener noreferrer"
              className="underline"
            >
              zotero.org/settings/keys
            </a>
            {' '}with read/write library access.
          </p>
        </div>

        {testResult?.success && (
          <p className="text-xs text-muted-foreground">
            Next, link a paper to a project and use <Link to="/projects" className="text-primary underline">Projects</Link>{' '}
            to organize its Zotero collection.
          </p>
        )}

        {/* Library ID */}
        <div className="space-y-2">
          <Label htmlFor="zotero-user-id">User ID</Label>
          <Input
            id="zotero-user-id"
            type="text"
            placeholder="e.g. 1234567"
            value={draftUserId ?? userId}
            onChange={(e) => setDraftUserId(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            Found at{' '}
            <a
              href="https://www.zotero.org/settings/keys"
              target="_blank"
              rel="noopener noreferrer"
              className="underline"
            >
              zotero.org/settings/keys
            </a>
            {' '}next to &quot;Your userID for use in API calls&quot;.
          </p>
        </div>

        {/* Library Type */}
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium leading-none">Library Type</legend>
          <div className="flex gap-4">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="radio"
                name="zotero-library-type"
                value="user"
                checked={libraryType === 'user'}
                onChange={() => handleLibraryTypeChange('user')}
                className="accent-primary"
              />
              Personal library
            </label>
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="radio"
                name="zotero-library-type"
                value="group"
                checked={libraryType === 'group'}
                onChange={() => handleLibraryTypeChange('group')}
                className="accent-primary"
              />
              Group library
            </label>
          </div>
          <p className="text-xs text-muted-foreground">
            Personal library = your own Zotero account (most people). Group library = a shared
            Zotero group — you&apos;ll also need its numeric Group ID.
          </p>
        </fieldset>

        {/* Group ID — visible only when library type is "group" */}
        {libraryType === 'group' && (
          <div className="space-y-2">
            <Label htmlFor="zotero-group-id">Group ID</Label>
            <Input
              id="zotero-group-id"
              type="text"
              inputMode="numeric"
              pattern="[0-9]*"
              placeholder="e.g. 987654"
              value={draftGroupId ?? groupIdRaw}
              onChange={(e) => setDraftGroupId(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              The numeric group ID from the Zotero group library URL
              (e.g.{' '}
              <code className="font-mono">zotero.org/groups/987654/...</code>
              ) or from{' '}
              <a
                href="https://www.zotero.org/settings/keys"
                target="_blank"
                rel="noopener noreferrer"
                className="underline"
              >
                zotero.org/settings/keys
              </a>
              {' '}next to the group library API path.
            </p>
          </div>
        )}

        {/* Test connection */}
        <div className="flex items-center gap-3">
          <Button
            variant="outline"
            size="sm"
            onClick={handleTestConnection}
            disabled={isTesting || !apiKey || !userId}
          >
            {isTesting ? (
              <Loader2 className="h-3 w-3 mr-1 animate-spin" />
            ) : null}
            {isTesting ? 'Testing…' : 'Test connection'}
          </Button>
          {testResult !== null && (
            <span className={`flex items-center gap-1 text-sm ${testResult.success ? 'text-[var(--status-ok)]' : 'text-destructive'}`}>
              {testResult.success ? (
                <CheckCircle className="h-4 w-4" />
              ) : (
                <XCircle className="h-4 w-4" />
              )}
              {testResult.success ? 'Connected' : (testResult.error ?? 'Failed')}
            </span>
          )}
        </div>

        {/* Auto-push on star */}
        <div className="flex items-center justify-between">
          <div>
            <Label htmlFor="zotero-auto-push" className="text-sm font-medium">Auto-push on star</Label>
            <p className="text-xs text-muted-foreground">
              Automatically push a paper to Zotero when you star it.
            </p>
          </div>
          <Switch
            id="zotero-auto-push"
            checked={autoPush}
            onCheckedChange={handleAutoPushChange}
            disabled={setMut.isPending}
          />
        </div>

        {/* Zotero → JARVIS sync */}
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <Label htmlFor="zotero-poll-enabled" className="text-sm font-medium">Enable Zotero → JARVIS sync</Label>
              <p className="text-xs text-muted-foreground">
                Automatically import new papers clipped into Zotero into JARVIS (checked hourly).
              </p>
            </div>
            <Switch
              id="zotero-poll-enabled"
              checked={pollEnabled}
              onCheckedChange={handlePollEnabledChange}
              disabled={setMut.isPending}
            />
          </div>

          {(pollEnabled || libraryScopeChanged) && (
            <div className="space-y-4 pl-1 border-l-2 border-muted ml-1">
              {/* Poll cron schedule */}
              {pollEnabled && (
                <div className="pl-4">
                  <ScheduleSelect
                    id="zotero-poll-schedule"
                    value={pollCron}
                    onChange={(cron) => setMut.mutate({ key: 'zotero.poll_cron', value: cron })}
                    disabled={setMut.isPending}
                  />
                </div>
              )}

              {libraryScopeChanged && (
                <p className="pl-4 text-xs text-muted-foreground">
                  Library identity changed. Queue a library sync now to import from the new Zotero scope.
                </p>
              )}

              {/* Sync now button */}
              <div className="pl-4">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleSyncNow}
                  disabled={isSyncing}
                >
                  {isSyncing ? (
                    <Loader2 className="h-3 w-3 mr-1 animate-spin" />
                  ) : null}
                  {isSyncing ? 'Syncing…' : libraryScopeChanged ? 'Run library sync now' : 'Sync now'}
                </Button>
              </div>
            </div>
          )}
        </div>

        {/* Advanced: deployment-wide network setting, not per-user */}
        <div className="space-y-2">
          <button
            type="button"
            className="flex w-full items-center gap-2 text-left text-sm font-medium"
            onClick={() => setAdvancedOpen((v) => !v)}
            aria-expanded={advancedOpen}
            aria-controls="zotero-advanced-settings"
          >
            {advancedOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
            Advanced
          </button>
          {advancedOpen && (
            <div id="zotero-advanced-settings" className="space-y-4 border-t pt-4">
              {pollEnabled && (
                <div className="space-y-2">
                  <Label htmlFor="zotero-poll-cron">Custom sync schedule (cron)</Label>
                  <Input
                    id="zotero-poll-cron"
                    type="text"
                    placeholder="0 * * * *"
                    value={draftPollCron ?? pollCron}
                    onChange={(e) => setDraftPollCron(e.target.value)}
                    className="font-mono text-sm"
                  />
                  <p className="text-xs text-muted-foreground">
                    For a schedule the picker above can&apos;t express. Saved with the Save
                    button below.
                  </p>
                </div>
              )}
              <div className="space-y-2">
                <Label htmlFor="zotero-allowed-private-hosts">Better BibTeX hosts</Label>
                <Input
                  id="zotero-allowed-private-hosts"
                  type="text"
                  placeholder="zotero.lan, 192.168.1.50"
                  value={draftAllowedHosts ?? allowedPrivateHosts}
                  onChange={(e) => setDraftAllowedHosts(e.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  Comma-separated hostnames on your own network allowed to serve Better BibTeX
                  citation keys. This permits connections to private-network destinations, so only
                  add hosts you control — it applies to every user on this deployment.{' '}
                  <code className="font-mono">host.docker.internal</code> is always allowed.
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Section-level Save for the typed fields above (API key, User ID, Group ID,
            custom cron, Better BibTeX hosts) — none of them save on blur. */}
        <div className="flex items-center gap-3 border-t pt-4">
          <Button onClick={handleSave} disabled={setMut.isPending || !hasDraftChanges}>
            {setMut.isPending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
