import { useState } from 'react';
import { errorMessage } from '@/lib/errors';
import { useQuery } from '@tanstack/react-query';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { MarkerCaption } from '@/components/typography/MarkerCaption';
import { DateRangeFilter } from '@/components/analytics/DateRangeFilter';
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
} from '@/lib/api';

function ChartSkeleton() {
  return (
    <div className="space-y-3">
      <Skeleton className="h-4 w-32" />
      <Skeleton className="h-[300px] w-full" />
    </div>
  );
}

export function AnalyticsPage() {
  const [days, setDays] = useState(30);

  const activityQuery = useQuery({
    queryKey: ['analytics', 'activity', days],
    queryFn: () => fetchAnalyticsActivity(days),
  });

  const retentionQuery = useQuery({
    queryKey: ['analytics', 'retention', days],
    queryFn: () => fetchAnalyticsRetention(days),
  });

  const reviewsQuery = useQuery({
    queryKey: ['analytics', 'reviews', days],
    queryFn: () => fetchAnalyticsReviews(days),
  });

  const llmCostQuery = useQuery({
    queryKey: ['analytics', 'llm-cost', days],
    queryFn: () => fetchAnalyticsLlmCost(days),
  });

  const sourceQuery = useQuery({
    queryKey: ['analytics', 'papers-by-source'],
    queryFn: fetchPapersBySource,
  });

  const statusQuery = useQuery({
    queryKey: ['analytics', 'papers-by-status'],
    queryFn: fetchPapersByStatus,
  });

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-[28px] leading-tight tracking-tight text-strong">Analytics</h1>
      </div>
      <p className="text-muted-foreground text-sm">Track your research activity and learning progress</p>

      <DateRangeFilter value={days} onChange={setDays} />

      {(activityQuery.isError || retentionQuery.isError || reviewsQuery.isError || llmCostQuery.isError || sourceQuery.isError || statusQuery.isError) && (
        <div className="py-8 text-center">
          <p className="text-sm text-destructive">
            Failed to load data:{' '}
            {(
              (activityQuery.error ?? retentionQuery.error ?? reviewsQuery.error ?? llmCostQuery.error ?? sourceQuery.error ?? statusQuery.error) as Error
            ).message}
          </p>
        </div>
      )}

      <MarkerCaption marker="ACTIVITY" />
      <div className="grid gap-6 md:grid-cols-2">
        <Card className="rounded-md border-hair shadow-none">
          <CardHeader>
            <CardTitle className="text-lg">Activity Overview</CardTitle>
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

      <MarkerCaption marker="DISTRIBUTION" />
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

      <MarkerCaption marker="REVIEWS" />
      <div className="grid gap-6 md:grid-cols-2">
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
