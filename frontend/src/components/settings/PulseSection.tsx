import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { toast } from 'sonner';
import {
  fetchConfig,
  setConfig,
  fetchPulseStats,
  getSystemCapabilities,
  apiFetch,
} from '@/lib/api';
import { useAuthStore } from '@/stores/auth-store';
import type { ConfigEntry, PulseStats } from '@/types';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { RejectedTopicsPanel } from '@/components/settings/RejectedTopicsPanel';
import { PulseScheduleCard } from './pulse/PulseScheduleCard';
import { PulseAdvancedTuningCard } from './pulse/PulseAdvancedTuningCard';
import { PulseRunStatusCard } from './pulse/PulseRunStatusCard';

// ---------------------------------------------------------------------------
// FavoriteTopicsPanel — small inline query component (not extracted: it's <30 LOC)
// ---------------------------------------------------------------------------

interface FeedbackSummaryItem {
  paper_id: number;
  title: string;
  count: number;
}

interface FeedbackSummary {
  top_positive: FeedbackSummaryItem[];
  top_negative: FeedbackSummaryItem[];
}

function FavoriteTopicsPanel() {
  const { data } = useQuery<FeedbackSummary>({
    queryKey: QUERY_KEYS.pulseHealth.feedback(),
    queryFn: () => apiFetch<FeedbackSummary>('/api/analytics/feedback-summary'),
    staleTime: 5 * 60_000,
  });
  if (!data?.top_positive.length) return null;
  return (
    <div className="space-y-2">
      <p className="text-sm font-medium">Papers you&apos;ve 👍 most</p>
      <ul className="space-y-1">
        {data.top_positive.slice(0, 5).map((item) => (
          <li key={item.paper_id} className="flex items-center justify-between text-sm">
            <span className="truncate text-muted-foreground">{item.title}</span>
            <span className="ml-2 tabular-nums text-[var(--status-ok)]">+{item.count}</span>
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">
        These papers are weighted higher in your research feed recommendations.
      </p>
    </div>
  );
}

const EMPTY_CONFIGS: ConfigEntry[] = [];

// ---------------------------------------------------------------------------
// PulseSection — top-level orchestrator
// ---------------------------------------------------------------------------

export function PulseSection() {
  const queryClient = useQueryClient();

  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === 'admin';

  const {
    data: configs,
    isLoading: configLoading,
    isError: configError,
  } = useQuery<ConfigEntry[]>({
    queryKey: QUERY_KEYS.config.all(),
    queryFn: fetchConfig,
  });
  const safeConfigs = configs ?? EMPTY_CONFIGS;

  const {
    data: stats,
    isLoading: statsLoading,
    isError: statsError,
  } = useQuery<PulseStats>({
    queryKey: QUERY_KEYS.pulse.statsAll(),
    queryFn: () => fetchPulseStats(),
    refetchInterval: 60_000,
  });

  const { data: capabilities } = useQuery({
    queryKey: QUERY_KEYS.config.systemCapabilities(),
    queryFn: getSystemCapabilities,
    staleTime: 5 * 60_000,
  });

  const hasNetworkx = capabilities?.networkx !== false;
  const hasSklearn = capabilities?.scikit_learn !== false;

  const setMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() }),
    onError: (err: Error) => {
      toast.error('Failed to update Pulse settings', {
        description: err.message,
      });
    },
  });

  const settingsUnavailable = configLoading || configError || configs === undefined;
  const statsUnavailable = statsLoading || statsError || stats === undefined;
  const settingsControlsDisabled = settingsUnavailable || setMut.isPending;

  return (
    <div className="space-y-6">
      <PulseScheduleCard
        configs={safeConfigs}
        setMut={setMut}
        settingsUnavailable={settingsUnavailable}
        settingsControlsDisabled={settingsControlsDisabled}
      />

      <PulseAdvancedTuningCard
        configs={safeConfigs}
        setMut={setMut}
        settingsControlsDisabled={settingsControlsDisabled}
        hasNetworkx={hasNetworkx}
        hasSklearn={hasSklearn}
      />

      {/* ── Favorite papers card ── */}
      <Card className="rounded-md border-hair shadow-none">
        <CardHeader>
          <CardTitle className="text-base">Papers you&apos;ve liked</CardTitle>
          <CardDescription>
            Papers with the most 👍 feedback. These raise the weight of related papers in your
            research feed.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <FavoriteTopicsPanel />
        </CardContent>
      </Card>

      {/* ── Rejected topics card ── */}
      <Card className="rounded-md border-hair shadow-none">
        <CardHeader>
          <CardTitle className="text-base">Topics you&apos;ve rejected</CardTitle>
          <CardDescription>
            Topics with the most 👎 feedback. Reset to allow recommendations from these topics
            again.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <RejectedTopicsPanel />
        </CardContent>
      </Card>

      <PulseRunStatusCard
        stats={stats}
        statsError={statsError}
        statsUnavailable={statsUnavailable}
        settingsUnavailable={settingsUnavailable}
        isAdmin={isAdmin}
      />
    </div>
  );
}
