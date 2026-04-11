import { useState, useEffect, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { fetchConfig, setConfig, triggerRecommendationRefresh } from '@/lib/api';

const DEFAULT_PULSE_WEIGHTS: Record<PulseWeightKey, number> = {
  embedding: 0.2,
  topic: 0.2,
  llm_relevance: 0.3,
  llm_novelty: 0.1,
  author_bonus: 0.15,
  recency: 0.05,
};

type PulseWeightKey =
  | 'embedding'
  | 'topic'
  | 'llm_relevance'
  | 'llm_novelty'
  | 'author_bonus'
  | 'recency';

const PULSE_WEIGHT_KEYS: PulseWeightKey[] = [
  'embedding',
  'topic',
  'llm_relevance',
  'llm_novelty',
  'author_bonus',
  'recency',
];

const PULSE_WEIGHT_LABELS: Record<PulseWeightKey, string> = {
  embedding: 'Embedding similarity',
  topic: 'Topic match',
  llm_relevance: 'LLM relevance',
  llm_novelty: 'LLM novelty',
  author_bonus: 'Tracked-author bonus',
  recency: 'Recency',
};

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

export function RecommendationSection() {
  const queryClient = useQueryClient();
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshMessage, setRefreshMessage] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  const { data: configs = [] } = useQuery({
    queryKey: ['config'],
    queryFn: fetchConfig,
  });

  const getConfigValue = (key: string, fallback: unknown) => {
    const entry = configs.find((c) => c.key === key);
    return entry !== undefined ? entry.value : fallback;
  };

  const enabled = getConfigValue('recommendation.enabled', true) as boolean;
  const likedWeight = Number(getConfigValue('recommendation.liked_weight', 0.6));
  const projectWeight = Number(getConfigValue('recommendation.project_weight', 0.4));
  const pulseWeights = coerceWeights(getConfigValue('pulse.weights', DEFAULT_PULSE_WEIGHTS));

  const [localLikedWeight, setLocalLikedWeight] = useState<number>(likedWeight);
  const [localProjectWeight, setLocalProjectWeight] = useState<number>(projectWeight);
  const [localPulseWeights, setLocalPulseWeights] =
    useState<Record<PulseWeightKey, number>>(pulseWeights);
  const pulseWeightsDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => { setLocalLikedWeight(likedWeight); }, [likedWeight]);
  useEffect(() => { setLocalProjectWeight(projectWeight); }, [projectWeight]);
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
  ]);
  useEffect(
    () => () => {
      if (pulseWeightsDebounceRef.current !== null) {
        clearTimeout(pulseWeightsDebounceRef.current);
      }
    },
    [],
  );

  const setMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['config'] }),
  });

  const save = (key: string, value: unknown) => setMut.mutate({ key, value });

  const updatePulseWeight = (key: PulseWeightKey, value: number) => {
    const next = { ...localPulseWeights, [key]: value };
    setLocalPulseWeights(next);
    if (pulseWeightsDebounceRef.current !== null) {
      clearTimeout(pulseWeightsDebounceRef.current);
    }
    pulseWeightsDebounceRef.current = setTimeout(() => {
      save('pulse.weights', next);
    }, 400);
  };

  const pulseWeightSum = PULSE_WEIGHT_KEYS.reduce((acc, k) => acc + localPulseWeights[k], 0);
  const pulseWeightSumOutOfRange = pulseWeightSum < 0.8 || pulseWeightSum > 1.2;

  const handleRefresh = async () => {
    setIsRefreshing(true);
    setRefreshMessage(null);
    setRefreshError(null);
    try {
      const result = await triggerRecommendationRefresh();
      setRefreshMessage(`Refreshed ${result.refreshed} recommendations`);
    } catch {
      setRefreshError('Refresh failed. Please try again.');
    } finally {
      setIsRefreshing(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Pulse & Recommendations</CardTitle>
        <CardDescription>
          Personalized paper suggestions and Pulse deck scoring weights
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="flex items-center justify-between">
          <Label>Enable recommendations</Label>
          <button
            type="button"
            role="switch"
            aria-checked={!!enabled}
            onClick={() => save('recommendation.enabled', !enabled)}
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

        <div className="space-y-2">
          <Label>Liked papers weight ({Math.round(localLikedWeight * 100)}%)</Label>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={localLikedWeight}
            onChange={(e) => setLocalLikedWeight(Number(e.target.value))}
            onPointerUp={() => save('recommendation.liked_weight', localLikedWeight)}
            className="w-full accent-primary"
          />
          <p className="text-xs text-muted-foreground">
            How much to weight similarity to papers you&apos;ve starred
          </p>
        </div>

        <div className="space-y-2">
          <Label>Project context weight ({Math.round(localProjectWeight * 100)}%)</Label>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={localProjectWeight}
            onChange={(e) => setLocalProjectWeight(Number(e.target.value))}
            onPointerUp={() => save('recommendation.project_weight', localProjectWeight)}
            className="w-full accent-primary"
          />
          <p className="text-xs text-muted-foreground">
            How much to weight relevance to your active projects
          </p>
        </div>

        <div className="space-y-3 border-t pt-4">
          <div className="flex items-center gap-1">
            <h3 className="text-sm font-semibold">Pulse scoring weights</h3>
            <InfoTooltip content="Weights applied to each signal when ranking Pulse candidate papers. Individual weights blend into a final score; values should roughly sum to 1.0." />
          </div>
          {PULSE_WEIGHT_KEYS.map((key) => (
            <div key={key} className="space-y-1">
              <Label className="flex items-center justify-between text-xs">
                <span>{PULSE_WEIGHT_LABELS[key]}</span>
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
          <p
            className={`text-xs ${
              pulseWeightSumOutOfRange ? 'text-amber-600' : 'text-muted-foreground'
            }`}
          >
            Sum: {pulseWeightSum.toFixed(2)}
            {pulseWeightSumOutOfRange && ' (target ~1.0)'}
          </p>
        </div>

        <div className="space-y-2">
          <Button variant="outline" onClick={handleRefresh} disabled={isRefreshing}>
            {isRefreshing ? 'Refreshing\u2026' : 'Refresh recommendations now'}
          </Button>
          {refreshMessage && (
            <p className="text-sm text-green-600">{refreshMessage}</p>
          )}
          {refreshError && (
            <p className="text-sm text-destructive">{refreshError}</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
