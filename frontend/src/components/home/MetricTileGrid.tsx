import { Link } from 'react-router-dom';
import {
  BookOpen,
  Eye,
  Clock,
  GraduationCap,
  FolderKanban,
  Tag,
  Bell,
} from 'lucide-react';
import { MetricTile } from '@/components/MetricTile';
import { Skeleton } from '@/components/ui/skeleton';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import type { DashboardMetrics } from '@/types';

interface MetricTileGridProps {
  metrics: DashboardMetrics | undefined;
  isLoading: boolean;
}

const tiles = [
  { key: 'total_papers' as const, title: 'Total Papers', icon: BookOpen, href: '/feed' },
  { key: 'unread_papers' as const, title: 'Unread Papers', icon: Eye, href: '/feed' },
  { key: 'pending_papers' as const, title: 'Unsummarized', icon: Clock, href: '/feed' },
  { key: 'due_cards' as const, title: 'Due Cards', icon: GraduationCap, href: '/cards' },
  { key: 'active_projects' as const, title: 'Active Projects', icon: FolderKanban, href: '/projects' },
  { key: 'topic_count' as const, title: 'Topics', icon: Tag, href: '/settings' },
  { key: 'nudge_count' as const, title: 'Nudges', icon: Bell, href: '/settings' },
] as const;

function SkeletonTile() {
  return (
    <Card>
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

export function MetricTileGrid({ metrics, isLoading }: MetricTileGridProps) {
  if (isLoading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {tiles.map((t) => (
          <SkeletonTile key={t.key} />
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
          />
        </Link>
      ))}
    </div>
  );
}
