import { useSearchParams } from 'react-router-dom';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { TopicSection } from '@/components/settings/TopicSection';
import { SourcesList } from '@/components/settings/SourcesList';
import { AuthorSection } from '@/components/settings/AuthorSection';
import { IngestionSection } from '@/components/settings/IngestionSection';
import { AutomationSection } from '@/components/settings/AutomationSection';
import { ExtractionTemplateSection } from '@/components/settings/ExtractionTemplateSection';
import { PulseSection } from '@/components/settings/PulseSection';
import { TimerSection } from '@/components/settings/TimerSection';
import { PairTelegram } from '@/components/setup/PairTelegram';
import { ZoteroSection } from '@/components/settings/ZoteroSection';
import { ProvidersSection } from '@/components/settings/ProvidersSection';
import { AppearanceSection } from '@/components/settings/AppearanceSection';

const VALID_TABS = new Set([
  'topics',
  'sources',
  'authors',
  'ingestion',
  'automation',
  'extraction',
  'pulse',
  'timer',
  'providers',
  'integrations',
  'appearance',
]);

export function SettingsPage() {
  // Wave 7 (C.1) — URL-synced tab. Audit B5 reported that deep-links into
  // /settings sub-sections did not preserve which tab was active. Fix: read
  // ?tab= from URL on mount; update URL on tab change so links are
  // shareable. Falls back to 'topics' for missing/invalid values.
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get('tab');
  const activeTab = requestedTab && VALID_TABS.has(requestedTab) ? requestedTab : 'topics';

  const handleTabChange = (value: string) => {
    const next = new URLSearchParams(searchParams);
    next.set('tab', value);
    setSearchParams(next, { replace: true });
  };

  return (
    <div className="space-y-6">
      <h1 className="text-3xl font-bold">Settings</h1>

      <Tabs value={activeTab} onValueChange={handleTabChange}>
        <TabsList className="overflow-x-auto scrollbar-thin flex-nowrap">
          <TabsTrigger value="topics">Topics</TabsTrigger>
          <TabsTrigger value="sources">Sources</TabsTrigger>
          <TabsTrigger value="authors">Authors</TabsTrigger>
          <TabsTrigger value="ingestion">Models & Preferences</TabsTrigger>
          <TabsTrigger value="automation">Automation</TabsTrigger>
          <TabsTrigger value="extraction">Extraction Templates</TabsTrigger>
          <TabsTrigger value="pulse">Pulse</TabsTrigger>
          <TabsTrigger value="timer">Timer</TabsTrigger>
          <TabsTrigger value="providers">Providers</TabsTrigger>
          <TabsTrigger value="integrations">Integrations</TabsTrigger>
          <TabsTrigger value="appearance">Appearance</TabsTrigger>
        </TabsList>

        <TabsContent value="topics">
          <TopicSection />
        </TabsContent>

        <TabsContent value="sources">
          <SourcesList />
        </TabsContent>

        <TabsContent value="authors">
          <AuthorSection />
        </TabsContent>

        <TabsContent value="ingestion">
          <IngestionSection />
        </TabsContent>

        <TabsContent value="automation">
          <AutomationSection />
        </TabsContent>

        <TabsContent value="extraction">
          <ExtractionTemplateSection />
        </TabsContent>

        <TabsContent value="pulse">
          <PulseSection />
        </TabsContent>

        <TabsContent value="timer">
          <TimerSection />
        </TabsContent>

        <TabsContent value="providers">
          <ProvidersSection />
        </TabsContent>

        <TabsContent value="appearance">
          <AppearanceSection />
        </TabsContent>

        <TabsContent value="integrations">
          <div className="space-y-8">
            <div className="space-y-4">
              <div>
                <h2 className="text-lg font-semibold">Telegram</h2>
                <p className="text-sm text-muted-foreground">
                  Pair a Telegram chat to receive briefings and interact with JARVIS from your phone.
                </p>
              </div>
              <PairTelegram />
            </div>
            <ZoteroSection />
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
