import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
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
        <h1 className="text-2xl font-bold">Analytics</h1>
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

      {/* Row 1: Activity + Retention */}
      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Activity Overview</CardTitle>
          </CardHeader>
          <CardContent>
            {activityQuery.isLoading ? (
              <ChartSkeleton />
            ) : activityQuery.isError ? (
              <p className="text-sm text-destructive">Failed to load: {(activityQuery.error as Error).message}</p>
            ) : activityQuery.data && activityQuery.data.length > 0 ? (
              <ActivityChart data={activityQuery.data} />
            ) : (
              <EmptyState title="No activity data" description="Start reading and processing papers to see your research analytics." />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Retention Trend</CardTitle>
          </CardHeader>
          <CardContent>
            {retentionQuery.isLoading ? (
              <ChartSkeleton />
            ) : retentionQuery.isError ? (
              <p className="text-sm text-destructive">Failed to load: {(retentionQuery.error as Error).message}</p>
            ) : retentionQuery.data && retentionQuery.data.length > 0 ? (
              <RetentionChart data={retentionQuery.data} />
            ) : (
              <EmptyState title="No retention data" description="Review some flashcards to see retention trends." />
            )}
          </CardContent>
        </Card>
      </div>

      {/* Row 2: Papers by Source + Status */}
      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Papers by Source</CardTitle>
          </CardHeader>
          <CardContent>
            {sourceQuery.isLoading ? (
              <ChartSkeleton />
            ) : sourceQuery.isError ? (
              <p className="text-sm text-destructive">Failed to load: {(sourceQuery.error as Error).message}</p>
            ) : sourceQuery.data && sourceQuery.data.length > 0 ? (
              <PapersBySourceChart data={sourceQuery.data} />
            ) : (
              <EmptyState title="No paper data" description="Ingest some papers to see source distribution." />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Papers by Status</CardTitle>
          </CardHeader>
          <CardContent>
            {statusQuery.isLoading ? (
              <ChartSkeleton />
            ) : statusQuery.isError ? (
              <p className="text-sm text-destructive">Failed to load: {(statusQuery.error as Error).message}</p>
            ) : statusQuery.data && statusQuery.data.length > 0 ? (
              <PapersByStatusChart data={statusQuery.data} />
            ) : (
              <EmptyState title="No status data" description="Papers will appear here once ingested." />
            )}
          </CardContent>
        </Card>
      </div>

      {/* Row 3: Reviews + LLM Cost */}
      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Reviews by Rating</CardTitle>
          </CardHeader>
          <CardContent>
            {reviewsQuery.isLoading ? (
              <ChartSkeleton />
            ) : reviewsQuery.isError ? (
              <p className="text-sm text-destructive">Failed to load: {(reviewsQuery.error as Error).message}</p>
            ) : reviewsQuery.data && reviewsQuery.data.length > 0 ? (
              <ReviewsByRatingChart data={reviewsQuery.data} />
            ) : (
              <EmptyState title="No review data" description="Submit some card reviews to see the distribution." />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-lg">LLM Cost Over Time</CardTitle>
          </CardHeader>
          <CardContent>
            {llmCostQuery.isLoading ? (
              <ChartSkeleton />
            ) : llmCostQuery.isError ? (
              <p className="text-sm text-destructive">Failed to load: {(llmCostQuery.error as Error).message}</p>
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
