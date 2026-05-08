import { ExternalLink } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';

const dashboardUrl = import.meta.env.VITE_LANGFUSE_PUBLIC_DASHBOARD as string | undefined;

export function LangfuseLinkCard() {
  const safeUrl = dashboardUrl?.startsWith('https://') ? dashboardUrl : null;
  if (!safeUrl) return null;

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardHeader>
        <CardTitle>Observability</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground mb-4">
          LLM call traces, latency, and token usage are tracked via Langfuse. Open the dashboard to
          inspect traces and monitor model performance.
        </p>
        <Button asChild variant="outline" size="sm">
          <a href={safeUrl} target="_blank" rel="noreferrer noopener">
            <ExternalLink className="h-4 w-4 mr-2" />
            Open Langfuse dashboard
          </a>
        </Button>
      </CardContent>
    </Card>
  );
}
