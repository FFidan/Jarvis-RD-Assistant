import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  fetchConfig,
  setConfig,
  fetchPulseStats,
  fetchPulseDebug,
  ApiError,
} from '@/lib/api';
import { cronToHumanReadable, cronToTime, timeToCron } from '@/lib/cron-utils';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Slider } from '@/components/ui/slider';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { TimeSelect } from '@/components/ui/time-select';
import { formatDate } from '@/lib/utils';
import { ChevronDown, ChevronRight, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { useJobStore } from '@/stores/job-store';
import type { ConfigEntry, PulseStats, PulseDebugInfo } from '@/types';
import { RejectedTopicsPanel } from '@/components/settings/RejectedTopicsPanel';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const CRON_TOOLTIP =
  'The time of day when Pulse discovery runs automatically. Papers are scored and ranked so your deck is ready when you start your day.';

const DEFAULT_PULSE_WEIGHTS: Record<PulseWeightKey, number> = {
  embedding: 0.2,
  topic: 0.2,
  llm_relevance: 0.3,
  llm_novelty: 0.1,
  author_bonus: 0.15,
  recency: 0.05,
  citation_pagerank: 0,
  citation_count: 0,
  citation_adamic_adar: 0,
  classifier: 0,
};

type PulseWeightKey =
  | 'embedding'
  | 'topic'
  | 'llm_relevance'
  | 'llm_novelty'
  | 'author_bonus'
  | 'recency'
  | 'citation_pagerank'
  | 'citation_count'
  | 'citation_adamic_adar'
  | 'classifier';

const PULSE_WEIGHT_KEYS: PulseWeightKey[] = [
  'embedding',
  'topic',
  'llm_relevance',
  'llm_novelty',
  'author_bonus',
  'recency',
  'citation_pagerank',
  'citation_count',
  'citation_adamic_adar',
  'classifier',
];

const PULSE_WEIGHT_LABELS: Record<PulseWeightKey, string> = {
  embedding: 'Embedding similarity',
  topic: 'Topic match',
  llm_relevance: 'LLM relevance',
  llm_novelty: 'LLM novelty',
  author_bonus: 'Tracked-author bonus',
  recency: 'Recency',
  citation_pagerank: 'Citation PageRank',
  citation_count: 'Citation count',
  citation_adamic_adar: 'Shared citation neighbourhood',
  classifier: 'Personal classifier',
};

const PULSE_WEIGHT_TOOLTIPS: Record<PulseWeightKey, string> = {
  embedding:
    "Semantic similarity between this paper and papers you've previously starred or rated. High weight = surface papers similar to what you already read.",
  topic:
    "Match between the paper's content and your configured research Topics. High weight = stay close to your declared research interests.",
  llm_relevance:
    'An LLM judges how relevant this paper is to your research focus. Slower but more accurate than keyword matching. High weight = quality over speed.',
  llm_novelty:
    "An LLM judges how novel or surprising this paper is given your reading history. High weight = prioritise papers you're unlikely to have already seen.",
  author_bonus:
    'Additive bonus for papers co-authored by anyone in your tracked Authors list. High weight = always surface papers by your followed researchers.',
  recency:
    'Prefer papers published more recently. High weight = always surface the newest work, even if it scores lower on relevance.',
  citation_pagerank:
    'Graph centrality inside the citation neighbourhood around candidate papers. Defaults off until you have enough citation data.',
  citation_count:
    'Normalized citation count from source metadata. Defaults off so it does not overpower relevance.',
  citation_adamic_adar:
    'Boosts candidates that share specific citation neighbours with papers you liked, without computing the full graph.',
  classifier:
    'Probability from the optional per-user classifier trained from Pulse ratings. Requires enough positive and negative feedback.',
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isValidCron(s: string): boolean {
  const parts = s.trim().split(/\s+/);
  if (parts.length !== 5) return false;
  return parts.every((p) => /^[*/0-9,\-]+$/.test(p));
}

function getConfigValue<T>(entries: ConfigEntry[], key: string, fallback: T): T {
  const entry = entries.find((c) => c.key === key);
  return entry !== undefined ? (entry.value as T) : fallback;
}

function coerceWeights(raw: unknown): Record<PulseWeightKey, number> {
  const out = { ...DEFAULT_PULSE_WEIGHTS };
  if (raw && typeof raw === 'object') {
    for (const key of PULSE_WEIGHT_KEYS) {
      const value = (raw as Record<string, unknown>)[key];
      if (typeof value === 'number' && Number.isFinite(value)) {
        out[key] = value;
      }
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Diagnostics collapsible sub-panel
// ---------------------------------------------------------------------------

function DiagnosticsPanel() {
  const [open, setOpen] = useState(false);
  const { data, isLoading, isError, refetch } = useQuery<PulseDebugInfo>({
    queryKey: ['pulse-debug'],
    queryFn: fetchPulseDebug,
    enabled: open,
    staleTime: 30_000,
  });

  return (
    <div className="rounded-md border">
      <button
        type="button"
        className="flex w-full items-center gap-2 p-3 text-sm font-medium hover:bg-muted/30 transition-colors"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        Diagnostics
      </button>

      {open && (
        <div className="border-t p-3 space-y-4">
          {isLoading && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading diagnostics…
            </div>
          )}
          {isError && (
            <div className="space-y-2">
              <p className="text-sm text-destructive">
                Failed to load diagnostics (no deck generated yet?).
              </p>
              <Button variant="outline" size="sm" onClick={() => void refetch()}>
                Retry
              </Button>
            </div>
          )}
          {data && (
            <>
              <div>
                <p className="text-xs font-semibold text-muted-foreground mb-1">
                  Deck: {data.deck_date} — {data.card_count} cards
                  {data.degraded_reason && (
                    <Badge variant="outline" className="ml-2 text-amber-600 border-amber-400">
                      {data.degraded_reason}
                    </Badge>
                  )}
                </p>
              </div>

              {/* Per-source counts */}
              {Object.keys(data.source_counts).length > 0 && (
                <div>
                  <p className="text-xs font-semibold mb-1">Source candidate counts</p>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs">
                    {Object.entries(data.source_counts).map(([src, count]) => (
                      <div key={src} className="flex justify-between">
                        <span className="text-muted-foreground">{src}</span>
                        <span className="font-mono">{count}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Topic embedding health */}
              {data.topic_embeddings.length > 0 && (
                <div>
                  <p className="text-xs font-semibold mb-1">Topic embedding health</p>
                  <div className="space-y-0.5 text-xs">
                    {data.topic_embeddings.map((te) => (
                      <div key={te.key} className="flex items-center gap-2">
                        <span
                          className={`inline-block h-2 w-2 rounded-full ${te.ok ? 'bg-green-500' : 'bg-red-500'}`}
                        />
                        <span className="font-mono text-muted-foreground truncate max-w-[200px]">
                          {te.key}
                        </span>
                        <span>{te.ok ? `dim=${te.dim}` : te.non_null ? 'wrong dim' : 'null'}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Top-N signals table */}
              {data.top_cards.length > 0 && (
                <div>
                  <p className="text-xs font-semibold mb-1">Top cards (rank order)</p>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs border-collapse">
                      <thead>
                        <tr className="text-muted-foreground text-left">
                          <th className="pr-2 pb-1 font-medium">Title</th>
                          <th className="pr-2 pb-1 font-mono font-medium">Score</th>
                          <th className="pr-2 pb-1 font-mono font-medium">Rel</th>
                          <th className="pb-1 font-mono font-medium">Nov</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.top_cards.map((card) => (
                          <tr key={card.card_id} className="border-t border-muted">
                            <td className="pr-2 py-0.5 max-w-[200px] truncate">{card.title}</td>
                            <td className="pr-2 py-0.5 font-mono">
                              {card.final_score.toFixed(3)}
                            </td>
                            <td className="pr-2 py-0.5 font-mono">
                              {card.llm_relevance !== null ? card.llm_relevance.toFixed(2) : '—'}
                            </td>
                            <td className="py-0.5 font-mono">
                              {card.llm_novelty !== null ? card.llm_novelty.toFixed(2) : '—'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main PulseSection
// ---------------------------------------------------------------------------

export function PulseSection() {
  const queryClient = useQueryClient();
  const cronTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const weightsDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (cronTimeoutRef.current !== null) clearTimeout(cronTimeoutRef.current);
      if (weightsDebounceRef.current !== null) clearTimeout(weightsDebounceRef.current);
    },
    [],
  );

  const { data: configs = [] } = useQuery({
    queryKey: ['config'],
    queryFn: fetchConfig,
  });

  const { data: stats } = useQuery<PulseStats>({
    queryKey: ['pulse-stats'],
    queryFn: () => fetchPulseStats(),
    refetchInterval: 60_000,
  });

  const setMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['config'] }),
  });

  // --- Config values ---
  const enabled = getConfigValue<boolean>(configs, 'pulse.enabled', false);
  const cron = getConfigValue<string>(configs, 'pulse.cron', '0 4 * * *');
  const deckSize = getConfigValue<number>(configs, 'pulse.deck_size', 10);
  const stage2TopK = getConfigValue<number>(configs, 'pulse.stage2_top_k', 50);
  const likedWeight = Number(getConfigValue(configs, 'recommendation.liked_weight', 0.6));
  const projectWeight = Number(getConfigValue(configs, 'recommendation.project_weight', 0.4));
  const l2LambdaConfig = Number(getConfigValue(configs, 'pulse.l2_lambda', 0.5));
  const pulseWeights = useMemo(
    () => coerceWeights(getConfigValue(configs, 'pulse.weights', DEFAULT_PULSE_WEIGHTS)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [configs],
  );

  const [localCron, setLocalCron] = useState(cron);
  const [localLikedWeight, setLocalLikedWeight] = useState(likedWeight);
  const [localProjectWeight, setLocalProjectWeight] = useState(projectWeight);
  const [l2Lambda, setL2Lambda] = useState(l2LambdaConfig);
  const [localPulseWeights, setLocalPulseWeights] =
    useState<Record<PulseWeightKey, number>>(pulseWeights);

  useEffect(() => { setLocalCron(cron); }, [cron]);
  useEffect(() => { setLocalLikedWeight(likedWeight); }, [likedWeight]);
  useEffect(() => { setLocalProjectWeight(projectWeight); }, [projectWeight]);
  useEffect(() => { setL2Lambda(l2LambdaConfig); }, [l2LambdaConfig]);
  useEffect(() => {
    setLocalPulseWeights(pulseWeights);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    pulseWeights.embedding,
    pulseWeights.topic,
    pulseWeights.llm_relevance,
    pulseWeights.llm_novelty,
    pulseWeights.author_bonus,
    pulseWeights.recency,
    pulseWeights.citation_pagerank,
    pulseWeights.citation_count,
    pulseWeights.citation_adamic_adar,
    pulseWeights.classifier,
  ]);

  const handleToggle = () => {
    setMut.mutate({ key: 'pulse.enabled', value: !enabled });
  };

  const handleCronChange = (value: string) => {
    setLocalCron(value);
    if (cronTimeoutRef.current) clearTimeout(cronTimeoutRef.current);
    if (!isValidCron(value)) return;
    cronTimeoutRef.current = setTimeout(() => {
      setMut.mutate({ key: 'pulse.cron', value });
    }, 400);
  };

  const updatePulseWeight = (key: PulseWeightKey, value: number) => {
    const next = { ...localPulseWeights, [key]: value };
    setLocalPulseWeights(next);
    if (weightsDebounceRef.current !== null) clearTimeout(weightsDebounceRef.current);
    weightsDebounceRef.current = setTimeout(() => {
      setMut.mutate({ key: 'pulse.weights', value: next });
    }, 400);
  };

  const pulseWeightSum = PULSE_WEIGHT_KEYS.reduce((acc, k) => acc + localPulseWeights[k], 0);
  const pulseWeightSumOutOfRange = pulseWeightSum < 0.8 || pulseWeightSum > 1.2;

  const handleNormalize = () => {
    const scale = 1 / pulseWeightSum;
    const next = { ...localPulseWeights };
    PULSE_WEIGHT_KEYS.forEach((k) => {
      next[k] = Math.round(localPulseWeights[k] * scale * 100) / 100;
    });
    setLocalPulseWeights(next);
    setMut.mutate({ key: 'pulse.weights', value: next });
  };

  // --- Job store for Generate Pulse ---
  const { startJob, hasRunning } = useJobStore();
  const isPulseRunning = hasRunning('pulse.generate');

  // --- Status badge ---
  let statusBadge: React.ReactNode = null;
  if (stats) {
    if (stats.last_error) {
      statusBadge = <Badge variant="destructive">Failed</Badge>;
    } else if (stats.decks_generated > 0) {
      statusBadge = <Badge variant="default" className="bg-green-600">OK</Badge>;
    } else {
      statusBadge = <Badge variant="outline">No decks yet</Badge>;
    }
  }

  return (
    <div className="space-y-6">
      {/* ── Schedule card ── */}
      <Card>
        <CardHeader>
          <CardTitle>Pulse</CardTitle>
          <CardDescription>
            Nightly ranked deck of candidate papers scored by the Pulse pipeline.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Enable toggle */}
          <div className="flex items-center justify-between">
            <Label htmlFor="pulse-enable-toggle">Enable Pulse</Label>
            <button
              id="pulse-enable-toggle"
              type="button"
              role="switch"
              aria-label="Enable Pulse"
              aria-checked={!!enabled}
              onClick={handleToggle}
              disabled={setMut.isPending}
              className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 ${
                enabled ? 'bg-primary' : 'bg-input'
              }`}
            >
              <span
                className={`pointer-events-none block h-5 w-5 rounded-full bg-background shadow-lg ring-0 transition-transform ${
                  enabled ? 'translate-x-5' : 'translate-x-0'
                }`}
              />
            </button>
          </div>

          {/* Daily run time */}
          <div className="space-y-1">
            <Label htmlFor="pulse-cron-time" className="flex items-center gap-1">
              Daily run time
              <InfoTooltip content={CRON_TOOLTIP} />
            </Label>
            <TimeSelect
              value={cronToTime(localCron)}
              onChange={(v) => handleCronChange(timeToCron(v, localCron))}
            />
            <p className="text-xs text-muted-foreground">{cronToHumanReadable(localCron)}</p>
          </div>

          {/* Deck size */}
          <div className="space-y-1">
            <Label htmlFor="pulse-deck-size" className="flex items-center justify-between">
              <span>Deck size</span>
              <span className="text-muted-foreground text-sm font-normal">{deckSize}</span>
            </Label>
            <input
              id="pulse-deck-size"
              type="range"
              min={5}
              max={30}
              step={5}
              value={deckSize}
              onChange={(e) =>
                setMut.mutate({ key: 'pulse.deck_size', value: parseInt(e.target.value, 10) })
              }
              disabled={setMut.isPending}
              className="w-full accent-primary"
            />
            <p className="text-xs text-muted-foreground">
              Papers in your daily Pulse deck. Larger decks = more variety but longer review.
            </p>
          </div>

          {/* Stage-2 ranking candidates */}
          <div className="space-y-1">
            <Label htmlFor="pulse-stage2-top-k" className="flex items-center justify-between">
              <span>Ranking candidates</span>
              <span className="text-muted-foreground text-sm font-normal">{stage2TopK}</span>
            </Label>
            <input
              id="pulse-stage2-top-k"
              type="range"
              min={20}
              max={100}
              step={10}
              value={stage2TopK}
              onChange={(e) =>
                setMut.mutate({ key: 'pulse.stage2_top_k', value: parseInt(e.target.value, 10) })
              }
              disabled={setMut.isPending}
              className="w-full accent-primary"
            />
            <p className="text-xs text-muted-foreground">
              Candidates the LLM reranker evaluates. Higher = better ranking quality but slower.
            </p>
          </div>
        </CardContent>
      </Card>

      {/* ── Scoring weights card ── */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Scoring weights</CardTitle>
          <CardDescription>
            Weights applied to each signal when ranking Pulse candidate papers.
            Values should roughly sum to 1.0.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* 6 weight sliders */}
          {PULSE_WEIGHT_KEYS.map((key) => (
            <div key={key} className="space-y-1">
              <Label className="flex items-center justify-between text-xs">
                <span className="flex items-center gap-1">
                  {PULSE_WEIGHT_LABELS[key]}
                  <InfoTooltip content={PULSE_WEIGHT_TOOLTIPS[key]} />
                </span>
                <span className="font-mono text-muted-foreground">
                  {localPulseWeights[key].toFixed(2)}
                </span>
              </Label>
              <input
                type="range"
                aria-label={`${PULSE_WEIGHT_LABELS[key]} weight`}
                min={0}
                max={1}
                step={0.05}
                value={localPulseWeights[key]}
                onChange={(e) => updatePulseWeight(key, Number(e.target.value))}
                className="w-full accent-primary"
              />
            </div>
          ))}
          <div className="flex items-center">
            <p
              className={`text-xs ${
                pulseWeightSumOutOfRange ? 'text-amber-600' : 'text-muted-foreground'
              }`}
            >
              Sum: {pulseWeightSum.toFixed(2)}
              {pulseWeightSumOutOfRange && ' (target ~1.0)'}
            </p>
            {pulseWeightSumOutOfRange && (
              <Button
                variant="outline"
                size="sm"
                onClick={handleNormalize}
                className="ml-2 h-6 px-2 text-xs"
              >
                Normalize to 1.0
              </Button>
            )}
          </div>

          {/* Liked-papers weight */}
          <div className="space-y-1 border-t pt-4">
            <Label className="flex items-center justify-between text-xs">
              <span className="flex items-center gap-1">
                Liked papers weight
                <InfoTooltip content="How much to weight similarity to papers you've starred when seeding Pulse discovery." />
              </span>
              <span className="font-mono text-muted-foreground">
                {Math.round(localLikedWeight * 100)}%
              </span>
            </Label>
            <input
              type="range"
              aria-label="Liked papers weight"
              min={0}
              max={1}
              step={0.05}
              value={localLikedWeight}
              onChange={(e) => setLocalLikedWeight(Number(e.target.value))}
              onPointerUp={() => setMut.mutate({ key: 'recommendation.liked_weight', value: localLikedWeight })}
              className="w-full accent-primary"
            />
          </div>

          {/* Project-context weight */}
          <div className="space-y-1">
            <Label className="flex items-center justify-between text-xs">
              <span className="flex items-center gap-1">
                Project context weight
                <InfoTooltip content="How much to weight relevance to your active projects when seeding Pulse discovery." />
              </span>
              <span className="font-mono text-muted-foreground">
                {Math.round(localProjectWeight * 100)}%
              </span>
            </Label>
            <input
              type="range"
              aria-label="Project context weight"
              min={0}
              max={1}
              step={0.05}
              value={localProjectWeight}
              onChange={(e) => setLocalProjectWeight(Number(e.target.value))}
              onPointerUp={() =>
                setMut.mutate({ key: 'recommendation.project_weight', value: localProjectWeight })
              }
              className="w-full accent-primary"
            />
          </div>
        </CardContent>
      </Card>

      {/* ── L2 negative-feedback penalty card ── */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">L2 negative-feedback penalty</CardTitle>
          <CardDescription>
            Strength of the cosine penalty applied to candidates similar to papers
            you&apos;ve thumbed-down. 0 disables the penalty; 1 = equal weight to positive
            examples; 2 = double weight. Default 0.5.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-4">
            <Slider
              min={0}
              max={2}
              step={0.05}
              value={[l2Lambda]}
              onValueChange={([v]) => setL2Lambda(v)}
              onValueCommit={([v]) =>
                setMut.mutate(
                  { key: 'pulse.l2_lambda', value: v },
                  {
                    onError: (err) =>
                      toast.error('Failed to update L2 lambda', {
                        description: err instanceof Error ? err.message : 'Unknown error',
                      }),
                  },
                )
              }
              className="flex-1"
            />
            <span className="font-mono text-sm w-12 text-right">{l2Lambda.toFixed(2)}</span>
          </div>
        </CardContent>
      </Card>

      {/* ── Rejected topics card ── */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Topics you&apos;ve rejected</CardTitle>
          <CardDescription>
            Topics with the most 👎 feedback. Reset to allow recommendations from
            these topics again.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <RejectedTopicsPanel />
        </CardContent>
      </Card>

      {/* ── Last Pulse run status card ── */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            Last Pulse run
            {statusBadge}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {stats ? (
            <div className="space-y-1 text-sm">
              <div className="flex justify-between">
                <span className="text-muted-foreground">Last run</span>
                <span className="font-mono">
                  {stats.last_run_at ? formatDate(stats.last_run_at) : 'never'}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">Decks generated</span>
                <span className="font-mono">{stats.decks_generated}</span>
              </div>
              {stats.last_error && (
                <div className="pt-1">
                  <Badge variant="destructive" className="text-xs">
                    {stats.last_error}
                  </Badge>
                </div>
              )}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">Loading stats…</p>
          )}

          <Button
            onClick={() => {
              startJob('pulse.generate', {}).catch((err: unknown) => {
                if (err instanceof ApiError && err.status === 429) {
                  toast.error('Rate limit reached. Try again in a minute.');
                } else {
                  toast.error('Failed to start Pulse generation.');
                }
              });
            }}
            disabled={isPulseRunning}
            className="w-full"
          >
            {isPulseRunning ? (
              <span className="flex items-center gap-2">
                <Loader2 className="h-4 w-4 animate-spin" />
                Generating…
              </span>
            ) : (
              'Generate Pulse now'
            )}
          </Button>

          <DiagnosticsPanel />
        </CardContent>
      </Card>
    </div>
  );
}
