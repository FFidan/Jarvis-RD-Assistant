import { Button } from '@/components/ui/button';
import { useMaintenanceStore } from '@/stores/maintenance-store';

interface QueryErrorStateProps {
  message?: string;
  onRetry?: () => void;
}

const MAINTENANCE_MESSAGE =
  'The app is temporarily read-only while a restore is running — this page will refresh automatically.';

export function QueryErrorState({
  message = "Couldn't load — check your connection and try again.",
  onRetry,
}: QueryErrorStateProps) {
  const maintenanceActive = useMaintenanceStore((s) => s.active);

  if (maintenanceActive) {
    // Retrying is automatic (MaintenanceBanner polls health) — no Retry button.
    return <div className="p-6 text-sm text-amber-700 dark:text-amber-400">{MAINTENANCE_MESSAGE}</div>;
  }

  return (
    <div className="p-6 text-sm text-destructive">
      {message}
      {onRetry && (
        <Button
          variant="ghost"
          size="sm"
          className="ml-3 text-destructive hover:text-destructive"
          onClick={onRetry}
        >
          Retry
        </Button>
      )}
    </div>
  );
}
