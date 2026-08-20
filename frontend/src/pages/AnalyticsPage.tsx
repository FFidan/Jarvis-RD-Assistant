import { useState } from 'react';
import { Link } from 'react-router-dom';
import { errorMessage } from '@/lib/errors';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { MarkerCaption } from '@/components/typography/MarkerCaption';
import { DateRangeFilter } from '@/components/analytics/DateRangeFilter';
import { KpiBand } from '@/components/analytics/KpiBand';
import { ActivityChart } from '@/components/analytics/ActivityChart';
import { RetentionChart } from '@/components/analytics/RetentionChart';
import { PapersBySourceChart } from '@/components/analytics/PapersBySourceChart';
import { PapersByStatusChart } from '@/components/analytics/PapersByStatusChart';
import { ReviewsByRatingChart } from '@/components/analytics/ReviewsByRatingChart';
import { LlmCostChart } from '@/components/analytics/LlmCostChart';
import { EmptyState } from '@/components/EmptyState';
import {
  fetchAnalyticsActivity,
  fetchAnalyticsRetention,
  fetchAnalyticsReviews,
  fetchAnalyticsLlmCost,
  fetchPapersBySource,
  fetchPapersByStatus,
  fetchAnalyticsSummary,
} from '@/lib/api';

function ChartSkeleton() {
  return (
    <div className="space-y-3">
      <Skeleton className="h-4 w-32" />
      <Skeleton className="h-[300px] w-full" />
    </div>
  );
}

function KpiBandSkeleton() {
  return (
    <div className="grid grid-cols-1 divide-y sm:grid-cols-3 sm:divide-x sm:divide-y-0 divide-hair border-y border-hair">
      {[0, 1, 2].map((i) => (
        <div key={i} className="min-w-0 px-4 py-4 first:pl-0 last:pr-0 space-y-2">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-10 w-16" />
          <Skeleton className="h-3 w-20" />
        </div>
      ))}
    </div>
  );
}

/**
 * Human-readable period start date: "since April 12" (locale month + day).
 * Used for the italic subtitle below the Reflect hero.
 */
function periodSinceLabel(days: number): string {
  const since = new Date(Date.now() - days * 86_400_000);
  return since.toLocaleDateString(undefined, { month: 'long', day: 'numeric' });
}

export function AnalyticsPage() {
  const [days, setDays] = useState(30);

  // ── summary (KPI band) ──────────────────────────────────────────────────
  // staleTime: 5 min — historical analytics data rarely changes mid-session;
  // revisits and `days` toggles read from cache instead of refetching.
  const summaryQuery = useQuery({
    queryKey: QUERY_KEYS.analytics.summary(days),
    queryFn: () => fetchAnalyticsSummary(days),
    staleTime: 5 * 60_000,
  });

  // ── existing chart queries (unchanged query keys + fetch fns) ───────────
  const activityQuery = useQuery({
    queryKey: QUERY_KEYS.analytics.activity(days),
    queryFn: () => fetchAnalyticsActivity(days),
    staleTime: 5 * 60_000,
  });

  const retentionQuery = useQuery({
    queryKey: QUERY_KEYS.analytics.retention(days),
    queryFn: () => fetchAnalyticsRetention(days),
    staleTime: 5 * 60_000,
  });

  const reviewsQuery = useQuery({
    queryKey: QUERY_KEYS.analytics.reviews(days),
    queryFn: () => fetchAnalyticsReviews(days),
    staleTime: 5 * 60_000,
  });

  const llmCostQuery = useQuery({
    queryKey: QUERY_KEYS.analytics.llmCost(days),
    queryFn: () => fetchAnalyticsLlmCost(days),
    staleTime: 5 * 60_000,
  });

  const sourceQuery = useQuery({
    queryKey: QUERY_KEYS.analytics.papersBySource(),
    queryFn: fetchPapersBySource,
    staleTime: 5 * 60_000,
  });

  const statusQuery = useQuery({
    queryKey: QUERY_KEYS.analytics.papersByStatus(),
    queryFn: fetchPapersByStatus,
    staleTime: 5 * 60_000,
  });

  const hasError =
    activityQuery.isError ||
    retentionQuery.isError ||
    reviewsQuery.isError ||
    llmCostQuery.isError ||
    sourceQuery.isError ||
    statusQuery.isError;

  const sinceLabel = periodSinceLabel(days);

  return (
    <div className="space-y-6 p-6">
      {/* ── Breadcrumb ─────────────────────────────────────────────────── */}
      <nav aria-label="breadcrumb" className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-faint">
        <Link to="/cards?mode=library" className="transition-colors hover:text-strong">
          Learn
        </Link>
        <span>/</span>
        <Link to="/analytics" className="text-meta hover:text-strong transition-colors">
          Analytics
        </Link>
      </nav>

      {/* ── § REVIEW · N DAYS marker + DateRangeFilter ─────────────────── */}
      {/*
        DateRangeFilter placement decision:
        Two options: (a) in the § REVIEW marker row or
        (b) below the hero as today. The marker-row variant is more compact and
        matches the mockup's "30 DAYS" label in the section header. However the
        button-group DateRangeFilter is wider than a compact dropdown and would
        overflow the marker row on smaller viewports. Resolution: place the
        DateRangeFilter directly below the hero block (option b), which keeps
        full discoverability and avoids layout overflow. The § REVIEW marker
        still shows the active `days` value inline as the "· N DAYS" suffix,
        satisfying the mockup's period-awareness requirement without crowding.
      */}
      <MarkerCaption marker={`REVIEW · ${days} DAYS`} />

      {/* ── Hero ───────────────────────────────────────────────────────── */}
      <div className="space-y-1">
        <h1 className="font-serif text-[2.5rem] leading-none tracking-tight text-strong">
          Analytics
        </h1>
        <p className="font-serif italic text-base text-muted-foreground">
          What you learned, and how you spent your time, since {sinceLabel}.
        </p>
      </div>

      {/* ── DateRangeFilter (below hero) ───────────────────────────────── */}
      <DateRangeFilter value={days} onChange={setDays} />

      {/* ── KPI band ───────────────────────────────────────────────────── */}
      {summaryQuery.isLoading ? (
        <KpiBandSkeleton />
      ) : summaryQuery.isError ? (
        <p className="text-sm text-destructive">
          Failed to load summary: {errorMessage(summaryQuery.error)}
        </p>
      ) : summaryQuery.data ? (
        <KpiBand data={summaryQuery.data} />
      ) : null}

      {/* ── Chart-level error banner ───────────────────────────────────── */}
      {hasError && (
        <div className="py-4 text-center">
          <p className="text-sm text-destructive">
            Failed to load data:{' '}
            {(
              (activityQuery.error ??
                retentionQuery.error ??
                reviewsQuery.error ??
                llmCostQuery.error ??
                sourceQuery.error ??
                statusQuery.error) as Error
            ).message}
          </p>
        </div>
      )}

      {/* ── § READING CADENCE ──────────────────────────────────────────── */}
      <MarkerCaption marker="READING CADENCE" />
      <div className="grid gap-6 md:grid-cols-2">
        <Card className="rounded-md border-hair shadow-none">
          <CardHeader>
            <CardTitle className="text-lg">Daily Activity</CardTitle>
          </CardHeader>
          <CardContent>
            {activityQuery.isLoading ? (
              <ChartSkeleton />
            ) : activityQuery.isError ? (
              <p className="text-sm text-destructive">Failed to load: {errorMessage(activityQuery.error)}</p>
            ) : activityQuery.data && activityQuery.data.length > 0 ? (
              <ActivityChart data={activityQuery.data} />
            ) : (
              <EmptyState title="No activity data" description="Start reading and processing papers to see your research analytics." />
            )}
          </CardContent>
        </Card>

        <Card className="rounded-md border-hair shadow-none">
          <CardHeader>
            <CardTitle className="flex items-center gap-1 text-lg">
              Retention Trend
              <InfoTooltip content="Probability of correctly recalling a flashcard at review time, averaged across your deck. 0.9 = you recall 90% of cards when they come due." />
            </CardTitle>
          </CardHeader>
          <CardContent>
            {retentionQuery.isLoading ? (
              <ChartSkeleton />
            ) : retentionQuery.isError ? (
              <p className="text-sm text-destructive">Failed to load: {errorMessage(retentionQuery.error)}</p>
            ) : retentionQuery.data && retentionQuery.data.length > 0 ? (
              <RetentionChart data={retentionQuery.data} />
            ) : (
              <EmptyState title="No retention data" description="Review some flashcards to see retention trends." />
            )}
          </CardContent>
        </Card>
      </div>

      {/* ── § LIBRARY ──────────────────────────────────────────────────── */}
      <MarkerCaption marker="LIBRARY" />
      <div className="grid gap-6 md:grid-cols-2">
        <Card className="rounded-md border-hair shadow-none">
          <CardHeader>
            <CardTitle className="text-lg">Papers by Source</CardTitle>
          </CardHeader>
          <CardContent>
            {sourceQuery.isLoading ? (
              <ChartSkeleton />
            ) : sourceQuery.isError ? (
              <p className="text-sm text-destructive">Failed to load: {errorMessage(sourceQuery.error)}</p>
            ) : sourceQuery.data && sourceQuery.data.length > 0 ? (
              <PapersBySourceChart data={sourceQuery.data} />
            ) : (
              <EmptyState title="No paper data" description="Ingest some papers to see source distribution." />
            )}
          </CardContent>
        </Card>

        <Card className="rounded-md border-hair shadow-none">
          <CardHeader>
            <CardTitle className="text-lg">Papers by Status</CardTitle>
          </CardHeader>
          <CardContent>
            {statusQuery.isLoading ? (
              <ChartSkeleton />
            ) : statusQuery.isError ? (
              <p className="text-sm text-destructive">Failed to load: {errorMessage(statusQuery.error)}</p>
            ) : statusQuery.data && statusQuery.data.length > 0 ? (
              <PapersByStatusChart data={statusQuery.data} />
            ) : (
              <EmptyState title="No status data" description="Papers will appear here once ingested." />
            )}
          </CardContent>
        </Card>
      </div>

      {/* ── § REVIEWS ──────────────────────────────────────────────────── */}
      <MarkerCaption marker="REVIEWS" />
      <div className="grid gap-6 md:grid-cols-1">
        <Card className="rounded-md border-hair shadow-none">
          <CardHeader>
            <CardTitle className="text-lg">Reviews by Rating</CardTitle>
          </CardHeader>
          <CardContent>
            {reviewsQuery.isLoading ? (
              <ChartSkeleton />
            ) : reviewsQuery.isError ? (
              <p className="text-sm text-destructive">Failed to load: {errorMessage(reviewsQuery.error)}</p>
            ) : reviewsQuery.data && reviewsQuery.data.length > 0 ? (
              <ReviewsByRatingChart data={reviewsQuery.data} />
            ) : (
              <EmptyState title="No review data" description="Submit some card reviews to see the distribution." />
            )}
          </CardContent>
        </Card>
      </div>

      {/* ── § COST ─────────────────────────────────────────────────────── */}
      <MarkerCaption marker="COST" />
      <div className="grid gap-6 md:grid-cols-1">
        <Card className="rounded-md border-hair shadow-none">
          <CardHeader>
            <CardTitle className="text-lg">LLM Cost Over Time</CardTitle>
          </CardHeader>
          <CardContent>
            {llmCostQuery.isLoading ? (
              <ChartSkeleton />
            ) : llmCostQuery.isError ? (
              <p className="text-sm text-destructive">Failed to load: {errorMessage(llmCostQuery.error)}</p>
            ) : llmCostQuery.data && llmCostQuery.data.length > 0 ? (
              <LlmCostChart data={llmCostQuery.data} />
            ) : (
              <EmptyState title="No LLM usage data" description="LLM costs will appear after summarization or card generation." />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
