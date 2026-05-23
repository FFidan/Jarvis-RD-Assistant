import { useQuery } from '@tanstack/react-query';
import { BookOpen } from 'lucide-react';
import { QUERY_KEYS } from '@/lib/query-keys';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { VerificationBadge } from '@/components/shared/VerificationBadge';
import { MarkerCaption as SectionHeader } from '@/components/typography/MarkerCaption';
import { fetchWeeklyDigest } from '@/lib/api';
import type { WeeklyDigestTheme, WeeklyDigestTopic } from '@/types';

/**
 * Weekly research digest section for My Day page.
 *
 * Shows per-topic theme clusters synthesised from papers the user engaged
 * with in the last 7 days. Each theme shows a VerificationBadge so the user
 * can see whether the LLM's theme text was verified against the source corpus.
 *
 * Data source: GET /api/digest/weekly (digest.weekly background job).
 */
export function WeeklyDigestSection() {
  const { data, isLoading, isError } = useQuery({
    queryKey: QUERY_KEYS.digest.weekly(),
    queryFn: () => fetchWeeklyDigest(7),
    staleTime: 30 * 60 * 1000, // 30 min — digest updates at most once a week
  });

  if (isLoading) {
    return (
      <section aria-label="Weekly digest" className="space-y-3">
        <SectionHeader marker="Weekly Digest" meta="last 7 days · engaged papers" />
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-28 w-full" />
      </section>
    );
  }

  if (isError) {
    return (
      <section aria-label="Weekly digest" className="space-y-3">
        <SectionHeader marker="Weekly Digest" meta="last 7 days · engaged papers" />
        <p className="text-sm text-muted-foreground">Could not load this week&apos;s digest.</p>
      </section>
    );
  }

  if (!data || data.topics.length === 0) {
    return (
      <section aria-label="Weekly digest" className="space-y-3">
        <SectionHeader marker="Weekly Digest" meta="last 7 days · engaged papers" />
        <p className="text-sm text-muted-foreground">
          No digest yet — engage with (save / read) papers this week to see themes here.
        </p>
      </section>
    );
  }

  return (
    <section aria-label="Weekly digest" className="space-y-3">
      <SectionHeader
        marker="Weekly Digest"
        meta={`last 7 days · ${data.total_papers} paper${data.total_papers !== 1 ? 's' : ''} · ${data.topics.length} topic${data.topics.length !== 1 ? 's' : ''}`}
      />
      <div className="space-y-4">
        {data.topics.map((topic) => (
          <DigestTopicBlock key={topic.name} topic={topic} />
        ))}
      </div>
    </section>
  );
}

interface DigestTopicBlockProps {
  topic: WeeklyDigestTopic;
}

function DigestTopicBlock({ topic }: DigestTopicBlockProps) {
  return (
    <Card>
      <CardHeader className="pb-2 pt-4">
        <CardTitle className="flex items-center gap-2 text-sm font-semibold">
          <BookOpen className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          {topic.name}
          <span className="ml-auto font-normal text-xs text-muted-foreground">
            {topic.paper_count} paper{topic.paper_count !== 1 ? 's' : ''}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 pb-4">
        {topic.summary && (
          <p className="text-sm text-muted-foreground">{topic.summary}</p>
        )}
        {topic.themes.length > 0 && (
          <ul className="space-y-1.5" aria-label={`Themes for ${topic.name}`}>
            {topic.themes.map((theme, idx) => (
              <DigestThemeRow key={idx} theme={theme} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

interface DigestThemeRowProps {
  theme: WeeklyDigestTheme;
}

function DigestThemeRow({ theme }: DigestThemeRowProps) {
  return (
    <li className="flex items-start gap-1.5 text-sm" data-testid="digest-theme-row">
      <span className="flex-1">{theme.theme}</span>
      {theme.verified === true && (
        <span className="mt-0.5 shrink-0" data-testid="digest-theme-verified">
          <VerificationBadge variant="verified" />
        </span>
      )}
      {theme.verified === false && (
        <span className="mt-0.5 shrink-0" data-testid="digest-theme-unverified">
          <VerificationBadge
            variant="unverified"
            reason={theme.verification_reason ?? 'Theme could not be verified against source papers'}
          />
        </span>
      )}
    </li>
  );
}
