import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchConfig, setConfig } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent } from '@/components/ui/card';
import { EmptyState } from '@/components/EmptyState';
import { Pencil, Check, X, Settings2 } from 'lucide-react';
import { ModelSelector } from '@/components/shared/ModelSelector';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import type { ConfigEntry } from '@/types';

// ---------------------------------------------------------------------------
// Cron <-> time helpers
// ---------------------------------------------------------------------------

/** Parse "M H * * *" cron string to "HH:MM" for <input type="time">. */
function cronToTime(cron: string): string {
  const parts = cron.split(/\s+/);
  try {
    const minute = parseInt(parts[0], 10);
    const hour = parseInt(parts[1], 10);
    if (isNaN(minute) || isNaN(hour)) return '09:00';
    return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
  } catch {
    return '09:00';
  }
}

/** Convert "HH:MM" back to "M H * * *" cron string. */
function timeToCron(time: string): string {
  const [hourStr, minuteStr] = time.split(':');
  const hour = parseInt(hourStr, 10);
  const minute = parseInt(minuteStr, 10);
  if (isNaN(hour) || isNaN(minute)) return '0 9 * * *';
  return `${minute} ${hour} * * *`;
}

/** Try to parse a config value as a notification config {cron, enabled}. */
function parseNotificationValue(
  value: unknown,
): { cron: string; enabled: boolean } | null {
  try {
    const obj = typeof value === 'string' ? JSON.parse(value) : value;
    if (
      obj &&
      typeof obj === 'object' &&
      typeof (obj as Record<string, unknown>).cron === 'string' &&
      typeof (obj as Record<string, unknown>).enabled === 'boolean'
    ) {
      return obj as { cron: string; enabled: boolean };
    }
    return null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Config metadata for human-readable labels and grouping
// ---------------------------------------------------------------------------

/** Keys that belong to other tabs (Pulse, Setup, Telegram) and should not
 *  appear in the "Models & Notifications" ingestion section. */
const HIDE_FROM_UI = new Set([
  'setup.completed',
  'telegram.owner_chat_id',
  'pulse.cron',
  'pulse.enabled',
  'pulse.deck_size',
  'pulse.stage2_top_k',
  'pulse.weights',
]);

const CONFIG_METADATA: Record<
  string,
  { label: string; description: string; group: string; tooltip?: string }
> = {
  'fsrs.desired_retention': {
    label: 'Target Retention',
    description: 'Desired probability of recalling a card correctly (0.0\u20131.0)',
    group: 'Spaced Repetition',
    tooltip:
      'Desired probability of recalling a card correctly at review time. 0.9 = 90% recall. Higher values = more frequent review sessions.',
  },
  'fsrs.learning_steps': {
    label: 'Learning Steps',
    description: 'Steps before a card graduates, as [min, max] minutes',
    group: 'Spaced Repetition',
    tooltip:
      "Minutes between a new card's first review attempts before it enters the FSRS long-term schedule. [1, 10] = reviewed after 1 min, then 10 min.",
  },
  'llm.embed_model': {
    label: 'Embedding Model',
    description: 'Model used for generating text embeddings',
    group: 'LLM Models',
  },
  'llm.fast_model': {
    label: 'Fast Model',
    description: 'Lightweight model for quick tasks like tagging and decomposition',
    group: 'LLM Models',
  },
  'llm.smart_model': {
    label: 'Smart Model',
    description: 'High-capability model for summarization, extraction, and RAG',
    group: 'LLM Models',
  },
  'paper.max_daily': {
    group: 'Paper Workflow',
    label: 'Max papers fetched per day',
    description: 'How many new papers to import in a single discovery run.',
  },
  'paper.auto_generate_cards': {
    group: 'Paper Workflow',
    label: 'Auto-generate flashcards after summarization',
    description:
      'Automatically generate spaced-repetition flashcards when a paper is summarized.',
  },
};

/** Format a config value for display. */
function formatConfigValue(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value);
}

/** Preferred order for groups (unlisted groups sort alphabetically after these). */
const GROUP_ORDER = ['LLM Models', 'Spaced Repetition', 'Paper Workflow', 'Other'];

// ---------------------------------------------------------------------------
// NotificationRow — inline time picker + enabled toggle
// ---------------------------------------------------------------------------

function NotificationRow({
  entry,
  onSave,
  isPending,
}: {
  entry: ConfigEntry;
  onSave: (key: string, value: unknown) => void;
  isPending: boolean;
}) {
  const parsed = parseNotificationValue(entry.value);
  if (!parsed) {
    // Fallback: render raw value (backward compatible)
    return null;
  }

  const timeValue = cronToTime(parsed.cron);

  const handleTimeChange = (newTime: string) => {
    onSave(entry.key, { cron: timeToCron(newTime), enabled: parsed.enabled });
  };

  const handleToggle = () => {
    onSave(entry.key, { cron: parsed.cron, enabled: !parsed.enabled });
  };

  return (
    <Card key={entry.key}>
      <CardContent className="flex items-center gap-4 p-4">
        <span className="shrink-0 font-mono text-sm">{entry.key}</span>
        <div className="flex flex-1 items-center gap-4">
          <div className="flex items-center gap-2">
            <Label htmlFor={`time-${entry.key}`} className="text-sm">
              Time
            </Label>
            <Input
              id={`time-${entry.key}`}
              type="time"
              value={timeValue}
              onChange={(e) => handleTimeChange(e.target.value)}
              disabled={isPending}
              className="w-32"
            />
          </div>
          <div className="flex items-center gap-2">
            <Label htmlFor={`toggle-${entry.key}`} className="text-sm">
              Enabled
            </Label>
            <button
              id={`toggle-${entry.key}`}
              type="button"
              role="switch"
              aria-checked={parsed.enabled}
              onClick={handleToggle}
              disabled={isPending}
              className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 ${
                parsed.enabled ? 'bg-primary' : 'bg-input'
              }`}
            >
              <span
                className={`pointer-events-none block h-5 w-5 rounded-full bg-background shadow-lg ring-0 transition-transform ${
                  parsed.enabled ? 'translate-x-5' : 'translate-x-0'
                }`}
              />
            </button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// IngestionSection
// ---------------------------------------------------------------------------

export function IngestionSection() {
  const queryClient = useQueryClient();
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [saveError, setSaveError] = useState<string | null>(null);

  const { data: configs = [], isLoading } = useQuery({
    queryKey: ['config'],
    queryFn: fetchConfig,
  });

  const setMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['config'] });
      queryClient.invalidateQueries({ queryKey: ['system-models'] });
      setEditingKey(null);
      setSaveError(null);
    },
    onError: (error: Error) => {
      setSaveError(`Failed to save: ${error.message}`);
    },
  });

  const startEdit = (entry: ConfigEntry) => {
    setEditingKey(entry.key);
    setEditValue(typeof entry.value === 'string' ? entry.value : JSON.stringify(entry.value));
  };

  const saveEdit = () => {
    if (!editingKey) return;
    let parsed: unknown = editValue;
    try {
      parsed = JSON.parse(editValue);
    } catch {
      // keep as string
    }
    setMut.mutate({ key: editingKey, value: parsed });
  };

  const handleNotificationSave = (key: string, value: unknown) => {
    setMut.mutate({ key, value });
  };

  if (isLoading) {
    return <div className="py-8 text-center text-muted-foreground">Loading config...</div>;
  }

  if (configs.length === 0) {
    return (
      <EmptyState
        title="No config entries"
        description="Ingestion config will appear here once set."
        icon={Settings2}
      />
    );
  }

  // Separate notification entries from regular config; hide keys owned by other tabs
  const notificationEntries = configs.filter((e) => e.key.startsWith('notifications.'));
  const regularEntries = configs
    .filter((e) => !e.key.startsWith('notifications.'))
    .filter((e) => !HIDE_FROM_UI.has(e.key));

  // Group regular configs by metadata group
  const grouped = regularEntries.reduce<Record<string, ConfigEntry[]>>((acc, entry) => {
    const group = CONFIG_METADATA[entry.key]?.group ?? 'Other';
    (acc[group] ??= []).push(entry);
    return acc;
  }, {});

  // Sort groups by preferred order
  const sortedGroups = Object.keys(grouped).sort((a, b) => {
    const ia = GROUP_ORDER.indexOf(a);
    const ib = GROUP_ORDER.indexOf(b);
    const oa = ia === -1 ? GROUP_ORDER.length : ia;
    const ob = ib === -1 ? GROUP_ORDER.length : ib;
    return oa - ob || a.localeCompare(b);
  });

  const renderEntry = (entry: ConfigEntry) => {
    const meta = CONFIG_METADATA[entry.key];
    const isLlm = entry.key.startsWith('llm.');

    // LLM model entries get a ModelSelector dropdown instead of a text input
    if (isLlm) {
      const rawValue = typeof entry.value === 'string' ? entry.value : JSON.stringify(entry.value);
      // Strip wrapping quotes from JSONB-encoded string values (e.g. '"qwen3:4b"' → 'qwen3:4b')
      const currentValue = rawValue.replace(/^"|"$/g, '');
      return (
        <Card key={entry.key}>
          <CardContent className="flex items-center gap-4 p-4">
            <div className="flex-1 min-w-0 space-y-2">
              <div className="font-medium text-sm">
                {meta?.label ?? entry.key}
              </div>
              {meta?.description && (
                <p className="text-xs text-muted-foreground">
                  {meta.description}
                </p>
              )}
              <ModelSelector
                value={currentValue}
                onChange={(v) => setMut.mutate({ key: entry.key, value: v })}
                role={entry.key}
              />
            </div>
          </CardContent>
        </Card>
      );
    }

    return (
      <Card key={entry.key}>
        <CardContent className="flex items-center gap-4 p-4">
          {editingKey === entry.key ? (
            <div className="flex-1 space-y-1">
              <div className="flex items-center gap-2">
                <span className="shrink-0 text-sm font-medium">
                  {meta?.label ?? entry.key}
                </span>
                <Input
                  value={editValue}
                  onChange={(e) => setEditValue(e.target.value)}
                  className="flex-1"
                />
                <Button
                  size="icon"
                  variant="ghost"
                  onClick={saveEdit}
                  disabled={setMut.isPending}
                >
                  <Check className="h-4 w-4" />
                </Button>
                <Button size="icon" variant="ghost" onClick={() => setEditingKey(null)}>
                  <X className="h-4 w-4" />
                </Button>
              </div>
              {saveError && (
                <p className="text-sm text-destructive mt-1">{saveError}</p>
              )}
            </div>
          ) : (
            <>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1 font-medium text-sm">
                  {meta?.label ?? entry.key}
                  {meta?.tooltip && <InfoTooltip content={meta.tooltip} />}
                </div>
                {meta?.description && (
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {meta.description}
                  </p>
                )}
                <span className="text-sm text-muted-foreground mt-1 block">
                  {formatConfigValue(entry.value)}
                </span>
              </div>
              <Button size="icon" variant="ghost" onClick={() => startEdit(entry)}>
                <Pencil className="h-4 w-4" />
              </Button>
            </>
          )}
        </CardContent>
      </Card>
    );
  };

  return (
    <div className="space-y-2">
      {/* Notification entries with time picker + toggle */}
      {notificationEntries.map((entry) => {
        const parsed = parseNotificationValue(entry.value);
        if (parsed) {
          return (
            <NotificationRow
              key={entry.key}
              entry={entry}
              onSave={handleNotificationSave}
              isPending={setMut.isPending}
            />
          );
        }
        // Fallback: render as regular entry
        return renderEntry(entry);
      })}

      {/* Grouped config entries with labels and descriptions */}
      {sortedGroups.map((group) => (
        <div key={group}>
          <h4 className="mt-4 mb-2 text-sm font-semibold text-muted-foreground first:mt-0">
            {group}
          </h4>
          <div className="space-y-2">
            {grouped[group].map(renderEntry)}
          </div>
        </div>
      ))}
    </div>
  );
}
