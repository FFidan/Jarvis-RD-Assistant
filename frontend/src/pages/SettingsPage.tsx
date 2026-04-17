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

export function SettingsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-3xl font-bold">Settings</h1>

      <Tabs defaultValue="topics">
        <TabsList className="overflow-x-auto scrollbar-thin flex-nowrap">
          <TabsTrigger value="topics">Topics</TabsTrigger>
          <TabsTrigger value="sources">Sources</TabsTrigger>
          <TabsTrigger value="authors">Authors</TabsTrigger>
          <TabsTrigger value="ingestion">Models & Notifications</TabsTrigger>
          <TabsTrigger value="automation">Automation</TabsTrigger>
          <TabsTrigger value="extraction">Extraction Templates</TabsTrigger>
          <TabsTrigger value="pulse">Pulse</TabsTrigger>
          <TabsTrigger value="timer">Timer</TabsTrigger>
          <TabsTrigger value="integrations">Integrations</TabsTrigger>
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

        <TabsContent value="integrations">
          <div className="space-y-4">
            <div>
              <h2 className="text-lg font-semibold">Telegram</h2>
              <p className="text-sm text-muted-foreground">
                Pair a Telegram chat to receive briefings and interact with JARVIS from your phone.
              </p>
            </div>
            <PairTelegram />
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
