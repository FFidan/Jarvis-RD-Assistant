import { useSearchParams } from 'react-router-dom';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { SectionHeader } from '@/components/my-day/sections/SectionHeader';
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
import { LangfuseLinkCard } from '@/components/settings/LangfuseLinkCard';

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
      <h1 className="text-[32px] leading-tight tracking-tight text-strong">Settings</h1>

      <Tabs value={activeTab} onValueChange={handleTabChange}>
        <TabsList className="bg-transparent border-b border-hair p-0 gap-2 overflow-x-auto scrollbar-thin flex-nowrap">
          <TabsTrigger className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none" value="topics">Topics</TabsTrigger>
          <TabsTrigger className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none" value="sources">Sources</TabsTrigger>
          <TabsTrigger className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none" value="authors">Authors</TabsTrigger>
          <TabsTrigger className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none" value="ingestion">Models & Preferences</TabsTrigger>
          <TabsTrigger className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none" value="automation">Automation</TabsTrigger>
          <TabsTrigger className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none" value="extraction">Extraction Templates</TabsTrigger>
          <TabsTrigger className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none" value="pulse">Pulse</TabsTrigger>
          <TabsTrigger className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none" value="timer">Timer</TabsTrigger>
          <TabsTrigger className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none" value="providers">Providers</TabsTrigger>
          <TabsTrigger className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none" value="integrations">Integrations</TabsTrigger>
          <TabsTrigger className="rounded-none px-3 py-2 -mb-px border-b-2 border-transparent data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong data-[state=active]:bg-transparent data-[state=active]:shadow-none" value="appearance">Appearance</TabsTrigger>
        </TabsList>

        <TabsContent value="topics">
          <SectionHeader marker="TOPICS" />
          <TopicSection />
        </TabsContent>

        <TabsContent value="sources">
          <SectionHeader marker="SOURCES" />
          <SourcesList />
        </TabsContent>

        <TabsContent value="authors">
          <SectionHeader marker="AUTHORS" />
          <AuthorSection />
        </TabsContent>

        <TabsContent value="ingestion">
          <SectionHeader marker="INGESTION" />
          <IngestionSection />
        </TabsContent>

        <TabsContent value="automation">
          <SectionHeader marker="AUTOMATION" />
          <AutomationSection />
        </TabsContent>

        <TabsContent value="extraction">
          <SectionHeader marker="EXTRACTION TEMPLATES" />
          <ExtractionTemplateSection />
        </TabsContent>

        <TabsContent value="pulse">
          <SectionHeader marker="PULSE" />
          <PulseSection />
        </TabsContent>

        <TabsContent value="timer">
          <SectionHeader marker="TIMER" />
          <TimerSection />
        </TabsContent>

        <TabsContent value="providers">
          <SectionHeader marker="PROVIDERS" />
          <div className="space-y-6">
            <ProvidersSection />
            <LangfuseLinkCard />
          </div>
        </TabsContent>

        <TabsContent value="appearance">
          <AppearanceSection />
        </TabsContent>

        <TabsContent value="integrations">
          <SectionHeader marker="INTEGRATIONS" />
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
            <SectionHeader marker="ZOTERO" />
            <ZoteroSection />
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
