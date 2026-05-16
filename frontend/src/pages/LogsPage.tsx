import { useSearchParams } from 'react-router-dom';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { LiveTab } from '@/components/logs/LiveTab';
import { JobsTab } from '@/components/logs/JobsTab';
import { SourcesTab } from '@/components/logs/SourcesTab';
import { EventsTab } from '@/components/logs/EventsTab';
import { AdminBreadcrumb } from '@/components/layout/AdminBreadcrumb';

const TABS = ['live', 'jobs', 'sources', 'events'] as const;
type TabId = typeof TABS[number];

export function LogsPage() {
  const [searchParams, setSearchParams] = useSearchParams();

  const tab = (searchParams.get('tab') as TabId | null) ?? 'live';
  const activeTab = TABS.includes(tab as TabId) ? tab : 'live';

  function handleTabChange(value: string) {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set('tab', value);
        return next;
      },
      { replace: true },
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <AdminBreadcrumb page="System logs" />
        <h1 className="text-2xl font-semibold tracking-tight">System Logs</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Live events, background jobs, source health, and event history
        </p>
      </div>

      <Tabs value={activeTab} onValueChange={handleTabChange}>
        <TabsList>
          <TabsTrigger value="live">Live</TabsTrigger>
          <TabsTrigger value="jobs">Jobs</TabsTrigger>
          <TabsTrigger value="sources">Sources</TabsTrigger>
          <TabsTrigger value="events">Events</TabsTrigger>
        </TabsList>

        <TabsContent value="live" className="mt-4">
          <LiveTab />
        </TabsContent>
        <TabsContent value="jobs" className="mt-4">
          <JobsTab />
        </TabsContent>
        <TabsContent value="sources" className="mt-4">
          <SourcesTab />
        </TabsContent>
        <TabsContent value="events" className="mt-4">
          <EventsTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
