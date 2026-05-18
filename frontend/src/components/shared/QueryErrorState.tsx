import { Button } from '@/components/ui/button';

interface QueryErrorStateProps {
  message?: string;
  onRetry?: () => void;
}

export function QueryErrorState({
  message = "Couldn't load — check your connection and try again.",
  onRetry,
}: QueryErrorStateProps) {
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
