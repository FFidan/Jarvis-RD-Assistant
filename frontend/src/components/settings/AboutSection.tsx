/**
 * AboutSection — Settings page footer showing the running app + server versions.
 *
 * FE version is the Vite build-time `__APP_VERSION__` define (frontend/package.json,
 * see vite.config.ts). BE version comes from the shared stack-health query
 * (`/health/internal`'s `version` field) — the same cache entry HealthDots and
 * MaintenanceBanner read, so mounting this never triggers an extra poll.
 *
 * When the server version is known and differs from the build the browser is
 * currently running, shows a reload hint: the server was redeployed but this
 * tab is still serving the previous build until refreshed.
 */
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { fetchStackHealth } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

export function AboutSection() {
  const { data } = useQuery({
    queryKey: QUERY_KEYS.stack.health(),
    queryFn: fetchStackHealth,
    retry: false,
  });

  const serverVersion = data?.version;
  // "unknown" means the server could not determine its own version (e.g. an
  // env-less deployment) — treat it as absent, not as a differing version, so it
  // renders "Unknown" and never triggers a false "update available" hint.
  const serverKnown = Boolean(serverVersion) && serverVersion !== 'unknown';
  const updateAvailable = serverKnown && serverVersion !== __APP_VERSION__;

  return (
    <Card className="rounded-md border-hair shadow-none" data-testid="about-section">
      <CardHeader>
        <CardTitle>About</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="text-sm text-muted-foreground">
          App version{' '}
          <span className="font-medium text-foreground" data-testid="about-fe-version">
            v{__APP_VERSION__}
          </span>
        </p>
        <p className="text-sm text-muted-foreground">
          Server version{' '}
          <span className="font-medium text-foreground" data-testid="about-be-version">
            {serverKnown ? `v${serverVersion}` : 'Unknown'}
          </span>
        </p>
        {updateAvailable && (
          <p
            className="text-xs text-amber-600 dark:text-amber-400"
            data-testid="about-update-hint"
          >
            An update is available — reload to finish updating.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
