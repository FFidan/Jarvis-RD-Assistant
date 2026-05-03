import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import { fetchFeed } from '@/lib/api';
import type { FeedResponse, FeedPaper } from '@/types';

export function HeroResumeReading() {
  const navigate = useNavigate();

  const { data, isLoading } = useQuery<FeedResponse>({
    queryKey: ['feed', 'reading', 'hero'],
    queryFn: () => fetchFeed({ view: 'library', filter: 'reading', limit: 20 }),
    staleTime: 60_000,
  });

  if (isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-5 w-48" />
        <Skeleton className="h-8 w-3/4" />
        <Skeleton className="h-4 w-full" />
      </div>
    );
  }

  const papers: FeedPaper[] = data?.papers ?? [];
  const reading = papers
    .filter((p) => p.state === 'reading')
    .sort((a, b) => {
      const aTime = a.user_state?.updated_at
        ? new Date(a.user_state.updated_at).getTime()
        : 0;
      const bTime = b.user_state?.updated_at
        ? new Date(b.user_state.updated_at).getTime()
        : 0;
      return bTime - aTime;
    })[0];

  if (!reading) {
    return (
      <p className="text-faint italic font-serif text-center py-8">
        No active reading — add a paper to your Reading List.
      </p>
    );
  }

  const authorsLine = reading.authors?.join(', ') ?? '';
  const publishedYear = reading.published_date
    ? new Date(reading.published_date).getFullYear()
    : null;

  return (
    <div className="space-y-4">
      {/* Header pill */}
      <div className="flex items-center gap-2">
        <span className="inline-flex items-center rounded-full bg-emerald-600 px-2.5 py-0.5 text-[10px] font-mono font-semibold text-white">
          Reading
        </span>
        {publishedYear && (
          <span className="font-mono text-[11px] text-faint">{publishedYear}</span>
        )}
      </div>

      {/* Title */}
      <h2
        className="font-serif text-[24px] leading-[1.18] tracking-tight max-w-[36ch] text-strong hover:text-[var(--ink-blue,#0b3a8a)] cursor-pointer transition-colors"
        onClick={() => navigate(`/paper/${reading.id}`)}
      >
        {reading.title}
      </h2>

      {/* Authors */}
      {authorsLine && (
        <p className="font-mono text-[11px] text-meta truncate">{authorsLine}</p>
      )}

      {/* CTA */}
      <div className="flex flex-wrap items-center gap-2 pt-1">
        <Button
          size="sm"
          className="bg-emerald-600 text-white hover:bg-emerald-700"
          onClick={() => navigate(`/paper/${reading.id}`)}
        >
          Resume reading
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => navigate('/feed?surface=library')}
        >
          All reading
        </Button>
      </div>
    </div>
  );
}
