import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { fetchConfig, setConfig, triggerRecommendationRefresh } from '@/lib/api';

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

  const [localLikedWeight, setLocalLikedWeight] = useState<number>(likedWeight);
  const [localProjectWeight, setLocalProjectWeight] = useState<number>(projectWeight);

  useEffect(() => { setLocalLikedWeight(likedWeight); }, [likedWeight]);
  useEffect(() => { setLocalProjectWeight(projectWeight); }, [projectWeight]);

  const setMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => setConfig(key, value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['config'] }),
  });

  const save = (key: string, value: unknown) => setMut.mutate({ key, value });

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
        <CardTitle>Recommendation Engine</CardTitle>
        <CardDescription>
          Personalized paper suggestions based on your reading history
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
