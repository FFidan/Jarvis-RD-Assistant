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
import { LangfuseLinkCard } from '@/components/settings/LangfuseLinkCard';
import { useAuthStore } from '@/stores/auth-store';

// Personal tab values — visible to every authenticated user.
const PERSONAL_TABS = new Set([
  'topics',
  'authors',
  'ingestion',
  'appearance',
  'integrations',
]);

// System tab values — visible to admin users only.
const SYSTEM_TABS = new Set([
  'sources',
  'automation',
  'extraction',
  'pulse',
  'timer',
  'providers',
]);

const ALL_VALID_TABS = new Set([...PERSONAL_TABS, ...SYSTEM_TABS]);

const TAB_TRIGGER_CLASS =
  'rounded-none px-3 py-2 -mb-px border-b-2 border-transparent ' +
  'data-[state=active]:border-[hsl(var(--ring))] data-[state=active]:text-strong ' +
  'data-[state=active]:bg-transparent data-[state=active]:shadow-none';

export function SettingsPage() {
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === 'admin';

  // Wave 7 (C.1) — URL-synced tab. Deep-links into /settings sub-sections
  // preserve the active tab. Falls back to 'topics' for missing/invalid values
  // or when a non-admin tries to access a system-only tab.
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get('tab');

  const activeTab = (() => {
    if (!requestedTab || !ALL_VALID_TABS.has(requestedTab)) return 'topics';
    // Redirect non-admins away from system tabs
    if (SYSTEM_TABS.has(requestedTab) && !isAdmin) return 'topics';
    return requestedTab;
  })();

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
          {/* Personal settings — always visible */}
          <TabsTrigger className={TAB_TRIGGER_CLASS} value="topics">Topics</TabsTrigger>
          <TabsTrigger className={TAB_TRIGGER_CLASS} value="authors">Authors</TabsTrigger>
          <TabsTrigger className={TAB_TRIGGER_CLASS} value="ingestion">Models & Preferences</TabsTrigger>
          <TabsTrigger className={TAB_TRIGGER_CLASS} value="integrations">Integrations</TabsTrigger>
          <TabsTrigger className={TAB_TRIGGER_CLASS} value="appearance">Appearance</TabsTrigger>

          {/* System settings — admin only */}
          {isAdmin && (
            <>
              <TabsTrigger className={TAB_TRIGGER_CLASS} value="sources">Sources</TabsTrigger>
              <TabsTrigger className={TAB_TRIGGER_CLASS} value="automation">Automation</TabsTrigger>
              <TabsTrigger className={TAB_TRIGGER_CLASS} value="extraction">Extraction Templates</TabsTrigger>
              <TabsTrigger className={TAB_TRIGGER_CLASS} value="pulse">Pulse</TabsTrigger>
              <TabsTrigger className={TAB_TRIGGER_CLASS} value="timer">Timer</TabsTrigger>
              <TabsTrigger className={TAB_TRIGGER_CLASS} value="providers">Providers</TabsTrigger>
            </>
          )}
        </TabsList>

        {/* Personal tab contents */}
        <TabsContent value="topics">
          <TopicSection />
        </TabsContent>

        <TabsContent value="authors">
          <AuthorSection />
        </TabsContent>

        <TabsContent value="ingestion">
          <IngestionSection />
        </TabsContent>

        <TabsContent value="appearance">
          <AppearanceSection />
        </TabsContent>

        <TabsContent value="integrations">
          <div className="space-y-8">
            <div className="space-y-4">
              <div>
                <p className="text-sm text-muted-foreground">
                  Pair a Telegram chat to receive briefings and interact with JARVIS from your phone.
                </p>
              </div>
              <PairTelegram />
            </div>
            <div className="space-y-4">
              <ZoteroSection />
            </div>
          </div>
        </TabsContent>

        {/* System tab contents — only rendered when admin */}
        {isAdmin && (
          <>
            <TabsContent value="sources">
              <SourcesList />
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
              <div className="space-y-6">
                <ProvidersSection />
                <LangfuseLinkCard />
              </div>
            </TabsContent>
          </>
        )}
      </Tabs>
    </div>
  );
}
