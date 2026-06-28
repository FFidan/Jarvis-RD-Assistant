import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { CheckCircle2, AlertTriangle, Loader2, XCircle } from 'lucide-react';
import { getSetupStatus } from '@/lib/api';

type Status = 'ok' | 'warn' | 'loading' | 'error';

function StatusIcon({ status }: { status: Status }) {
  if (status === 'ok') return <CheckCircle2 className="h-5 w-5 text-green-500" />;
  if (status === 'warn') return <AlertTriangle className="h-5 w-5 text-yellow-500" />;
  if (status === 'loading') return <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />;
  return <XCircle className="h-5 w-5 text-destructive" />;
}

function Row({
  status,
  label,
  hint,
}: {
  status: Status;
  label: string;
  hint?: string;
}) {
  return (
    <div className="flex items-start gap-3 rounded-md border p-3">
      <StatusIcon status={status} />
      <div className="flex-1">
        <p className="font-medium">{label}</p>
        {hint && <p className="text-sm text-muted-foreground">{hint}</p>}
      </div>
    </div>
  );
}

/**
 * Wizard step 2: polls /api/system/setup-status every 5s and shows the
 * readiness state of Postgres, models, topics, and Telegram.
 * Non-blocking — always allow "Next".
 */
export function SystemCheck() {
  const { data, isLoading, isError } = useQuery({
    queryKey: QUERY_KEYS.setup.status(),
    queryFn: getSetupStatus,
    refetchInterval: 5000,
    staleTime: 0,
  });

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        Checking system status...
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="text-sm text-destructive">
        Unable to reach the API. Make sure paper_ingestion is running and your API key is correct.
      </div>
    );
  }

  const modelStatus: Status = data.models_ready
    ? 'ok'
    : data.models_downloading.length > 0
      ? 'loading'
      : 'warn';
  const modelLabel = data.models_ready
    ? 'Models ready'
    : data.models_downloading.length > 0
      ? `Still pulling: ${data.models_downloading.join(', ')}`
      : 'Models not ready';
  const modelHint = !data.models_ready && data.models_downloading.length === 0
    ? 'Ollama not reachable yet, or models still provisioning — check `docker compose logs ollama-bootstrap`.'
    : undefined;

  const topicsStatus: Status = data.topics_count > 0 ? 'ok' : 'warn';
  const topicsLabel =
    data.topics_count > 0
      ? `${data.topics_count} research ${data.topics_count === 1 ? 'topic' : 'topics'} configured`
      : 'No topics yet (you can add one in the next step)';

  let telegramStatus: Status = 'warn';
  let telegramLabel = 'Telegram not configured (optional)';
  if (data.telegram_configured && data.telegram_paired) {
    telegramStatus = 'ok';
    telegramLabel = 'Telegram paired';
  } else if (data.telegram_configured && !data.telegram_paired) {
    telegramStatus = 'warn';
    telegramLabel = 'Telegram bot configured but not paired';
  }

  return (
    <div className="space-y-2">
      <Row status="ok" label="API & database" hint="Postgres reachable" />
      <Row status={modelStatus} label={modelLabel} hint={modelHint ?? 'Ollama / LiteLLM'} />
      <Row status={topicsStatus} label={topicsLabel} />
      <Row status={telegramStatus} label={telegramLabel} />
      {(data.model_warnings ?? []).map((w) => (
        <Row key={w} status="warn" label={w} />
      ))}
    </div>
  );
}
