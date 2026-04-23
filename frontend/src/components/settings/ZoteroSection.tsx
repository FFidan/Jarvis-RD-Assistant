import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchConfig, setConfig, zoteroTest, zoteroPollNow } from '@/lib/api';
import { useJobStore } from '@/stores/job-store';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { CheckCircle, XCircle, Loader2 } from 'lucide-react';
import type { ConfigEntry } from '@/types';

const ZOTERO_KEYS = ['zotero.api_key', 'zotero.user_id', 'zotero.library_type', 'zotero.auto_push_on_star'] as const;
type ZoteroKey = (typeof ZOTERO_KEYS)[number];

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

  const { data: configs = [], isLoading } = useQuery({
    queryKey: ['config'],
    queryFn: fetchConfig,
  });

  // Derived values from config
  const apiKey = getConfigValue(configs, 'zotero.api_key');
  const userId = getConfigValue(configs, 'zotero.user_id');
  const libraryType = getConfigValue(configs, 'zotero.library_type') || 'user';
  const autoPush = getConfigValue(configs, 'zotero.auto_push_on_star') === 'true';
  const pollEnabled = getConfigValue(configs, 'zotero.poll_enabled') === 'true';
  const pollCron = getConfigValue(configs, 'zotero.poll_cron') || '';

  // Local draft state for text inputs (saved on blur)
  const [draftApiKey, setDraftApiKey] = useState<string | null>(null);
  const [draftUserId, setDraftUserId] = useState<string | null>(null);
  const [draftPollCron, setDraftPollCron] = useState<string | null>(null);

  // Test connection state
  const [testResult, setTestResult] = useState<{ success: boolean; error?: string } | null>(null);
  const [isTesting, setIsTesting] = useState(false);

  // Sync now state
  const [isSyncing, setIsSyncing] = useState(false);

  const setMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['config'] });
    },
  });

  const handleBlurApiKey = () => {
    if (draftApiKey !== null && draftApiKey !== apiKey) {
      setMut.mutate({ key: 'zotero.api_key', value: draftApiKey });
    }
    setDraftApiKey(null);
  };

  const handleBlurUserId = () => {
    if (draftUserId !== null && draftUserId !== userId) {
      setMut.mutate({ key: 'zotero.user_id', value: draftUserId });
    }
    setDraftUserId(null);
  };

  const handleBlurPollCron = () => {
    if (draftPollCron !== null && draftPollCron !== pollCron) {
      setMut.mutate({ key: 'zotero.poll_cron', value: draftPollCron });
    }
    setDraftPollCron(null);
  };

  const handleLibraryTypeChange = (type: 'user' | 'group') => {
    setMut.mutate({ key: 'zotero.library_type', value: type });
  };

  const handleAutoPushChange = (checked: boolean) => {
    setMut.mutate({ key: 'zotero.auto_push_on_star', value: String(checked) });
  };

  const handlePollEnabledChange = (checked: boolean) => {
    setMut.mutate({ key: 'zotero.poll_enabled', value: String(checked) });
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
      useJobStore.getState().trackExternalJob({
        jobId: response.job_id,
        kind: 'zotero.poll',
        payload: {},
        status: response.status as 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled',
      });
    } catch {
      // silently ignore — job may have queued anyway
    } finally {
      setIsSyncing(false);
    }
  };

  if (isLoading) {
    return <div className="py-4 text-sm text-muted-foreground">Loading Zotero settings…</div>;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Zotero</CardTitle>
        <p className="text-sm text-muted-foreground">
          Connect JARVIS to your Zotero library to push papers and copy citation keys.
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
            onBlur={handleBlurApiKey}
            autoComplete="off"
          />
          <p className="text-xs text-muted-foreground">
            Generate a key at{' '}
            <a
              href="https://www.zotero.org/settings/keys"
              target="_blank"
              rel="noreferrer"
              className="underline"
            >
              zotero.org/settings/keys
            </a>
            {' '}with read/write library access.
          </p>
        </div>

        {/* User ID */}
        <div className="space-y-2">
          <Label htmlFor="zotero-user-id">User ID</Label>
          <Input
            id="zotero-user-id"
            type="text"
            placeholder="e.g. 1234567"
            value={draftUserId ?? userId}
            onChange={(e) => setDraftUserId(e.target.value)}
            onBlur={handleBlurUserId}
          />
          <p className="text-xs text-muted-foreground">
            Found at{' '}
            <a
              href="https://www.zotero.org/settings/keys"
              target="_blank"
              rel="noreferrer"
              className="underline"
            >
              zotero.org/settings/keys
            </a>
            {' '}next to "Your userID for use in API calls".
          </p>
        </div>

        {/* Library Type */}
        <div className="space-y-2">
          <Label>Library Type</Label>
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
        </div>

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
            <span className={`flex items-center gap-1 text-sm ${testResult.success ? 'text-green-600 dark:text-green-400' : 'text-destructive'}`}>
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
            <Label className="text-sm font-medium">Auto-push on star</Label>
            <p className="text-xs text-muted-foreground">
              Automatically push a paper to Zotero when you star it.
            </p>
          </div>
          <Switch
            checked={autoPush}
            onCheckedChange={handleAutoPushChange}
            disabled={setMut.isPending}
          />
        </div>

        {/* Zotero → JARVIS sync */}
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <Label className="text-sm font-medium">Enable Zotero → JARVIS sync</Label>
              <p className="text-xs text-muted-foreground">
                Automatically import new papers clipped into Zotero into JARVIS (checked hourly).
              </p>
            </div>
            <Switch
              checked={pollEnabled}
              onCheckedChange={handlePollEnabledChange}
              disabled={setMut.isPending}
            />
          </div>

          {pollEnabled && (
            <div className="space-y-4 pl-1 border-l-2 border-muted ml-1">
              {/* Poll cron schedule */}
              <div className="space-y-1 pl-4">
                <Label htmlFor="zotero-poll-cron" className="text-sm">Sync schedule (cron)</Label>
                <Input
                  id="zotero-poll-cron"
                  type="text"
                  placeholder="0 * * * *"
                  value={draftPollCron ?? pollCron}
                  onChange={(e) => setDraftPollCron(e.target.value)}
                  onBlur={handleBlurPollCron}
                  className="font-mono text-sm"
                />
                <p className="text-xs text-muted-foreground">Default: hourly (0 * * * *)</p>
              </div>

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
                  {isSyncing ? 'Syncing…' : 'Sync now'}
                </Button>
              </div>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
