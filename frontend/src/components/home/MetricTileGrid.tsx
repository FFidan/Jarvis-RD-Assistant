import { Link } from 'react-router-dom';
import {
  BookOpen,
  GraduationCap,
  FolderKanban,
  Tag,
  Bell,
  AlertTriangle,
} from 'lucide-react';
import { MetricTile } from '@/components/MetricTile';
import { Skeleton } from '@/components/ui/skeleton';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import type { DashboardMetrics } from '@/types';

interface MetricTileGridProps {
  metrics: DashboardMetrics | undefined;
  isLoading: boolean;
  isError?: boolean;
}

const SKELETON_COUNT = 5;

function SkeletonTile() {
  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-4 w-4" />
      </CardHeader>
      <CardContent>
        <Skeleton className="h-8 w-16" />
      </CardContent>
    </Card>
  );
}

export function MetricTileGrid({ metrics, isLoading, isError }: MetricTileGridProps) {
  const unread = metrics?.unread_papers ?? 0;
  const pending = metrics?.pending_papers ?? 0;
  const librarySubtitle =
    unread > 0 ? `${unread} unread · ${pending} unsummarized` : 'All caught up';

  const tiles = [
    {
      key: 'total_papers' as const,
      title: 'Papers',
      subtitle: librarySubtitle,
      icon: BookOpen,
      href: '/feed',
    },
    { key: 'due_cards' as const, title: 'Due Cards', icon: GraduationCap, href: '/cards' },
    {
      key: 'active_projects' as const,
      title: 'Active Projects',
      icon: FolderKanban,
      href: '/projects',
    },
    { key: 'topic_count' as const, title: 'Topics', icon: Tag, href: '/settings' },
    {
      key: 'nudge_count' as const,
      title: 'Scheduled Jobs',
      icon: Bell,
      href: '/settings',
    },
  ];

  if (isError) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-hair p-4 text-sm text-destructive">
        <AlertTriangle className="h-4 w-4 shrink-0" />
        <span>Dashboard unavailable — retry later or refresh the page.</span>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {Array.from({ length: SKELETON_COUNT }).map((_, i) => (
          <SkeletonTile key={`skeleton-${i}`} />
        ))}
      </div>
    );
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {tiles.map((t) => (
        <Link key={t.key} to={t.href} className="transition-transform hover:scale-[1.02]">
          <MetricTile
            title={t.title}
            value={metrics?.[t.key] ?? 0}
            icon={t.icon}
            subtitle={t.subtitle}
          />
        </Link>
      ))}
    </div>
  );
}
