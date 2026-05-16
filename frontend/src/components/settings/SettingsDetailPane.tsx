/**
 * SettingsDetailPane — right-hand pane of the 2-pane Settings IA.
 *
 * Renders:
 *  1. Breadcrumb ("Settings / <Section title> / <item label>")
 *  2. Section heading (serif text-3xl)
 *  3. The mounted section component for the active (section, item) pair
 */
import { AccountSection } from './AccountSection';
import { AppearanceSection } from './AppearanceSection';
import { IngestionSection } from './IngestionSection';
import { SpacedRepetitionSection } from './SpacedRepetitionSection';
import { ProvidersSection } from './ProvidersSection';
import { LangfuseLinkCard } from './LangfuseLinkCard';
import { AutomationSection } from './AutomationSection';
import { ExtractionTemplateSection } from './ExtractionTemplateSection';
import { PulseSection } from './PulseSection';
import { TimerSection } from './TimerSection';
import { TelegramPairingSection } from './TelegramPairingSection';
import { PairTelegram } from '@/components/setup/PairTelegram';
import { ZoteroSection } from './ZoteroSection';
import { TopicSection } from './TopicSection';
import { AuthorSection } from './AuthorSection';
import { SourceDetailPane } from './SourceDetailPane';
import { SourcesList } from './SourcesList';
import { SmtpSection } from './SmtpSection';

// ---------------------------------------------------------------------------
// Section title map (mirrors STATIC_SECTIONS in SettingsRail)
// ---------------------------------------------------------------------------

const SECTION_TITLES: Record<string, string> = {
  account: 'Account',
  sources: 'Sources',
  models: 'Models',
  system: 'System',
  integrations: 'Integrations',
  research: 'Research',
};

const ITEM_LABELS: Record<string, Record<string, string>> = {
  account: {
    profile: 'Profile & Email',
    appearance: 'Appearance',
  },
  sources: {
    sources: 'Sources',
  },
  models: {
    llm: 'LLM Models',
    providers: 'Cloud Providers',
  },
  system: {
    automation: 'Automation',
    extraction: 'Extraction Templates',
    smtp: 'Email / SMTP',
    pulse: 'Pulse',
    timer: 'Timer',
    observability: 'Observability',
  },
  integrations: {
    telegram: 'Telegram',
    zotero: 'Zotero',
  },
  research: {
    topics: 'Topics',
    authors: 'Authors',
    'spaced-repetition': 'Spaced Repetition',
  },
};

// ---------------------------------------------------------------------------
// Breadcrumb
// ---------------------------------------------------------------------------

function Breadcrumb({
  sectionTitle,
  itemLabel,
}: {
  sectionTitle: string;
  itemLabel: string;
}) {
  return (
    <nav aria-label="breadcrumb" className="flex items-center gap-1.5 text-xs text-muted-foreground mb-4">
      <span>Settings</span>
      <span aria-hidden>/</span>
      <span>{sectionTitle}</span>
      <span aria-hidden>/</span>
      <span className="text-foreground font-medium">{itemLabel}</span>
    </nav>
  );
}

// ---------------------------------------------------------------------------
// Content router — maps (section, item) → component
// ---------------------------------------------------------------------------

function DetailContent({
  section,
  item,
}: {
  section: string;
  item: string;
}) {
  if (section === 'account') {
    if (item === 'profile') return <AccountSection />;
    if (item === 'appearance') return <AppearanceSection />;
  }

  if (section === 'sources') {
    // Single "Sources" item shows the full all-sources list (enable/disable/reorder/api-key).
    // Deep-links with a specific source_type slug still work via SourceDetailPane.
    if (item === 'sources' || item === '') return <SourcesList />;
    return <SourceDetailPane sourceType={item} />;
  }

  if (section === 'models') {
    if (item === 'llm') return <IngestionSection />;
    if (item === 'providers') {
      return (
        <div className="space-y-6">
          <ProvidersSection />
        </div>
      );
    }
  }

  if (section === 'system') {
    if (item === 'automation') return <AutomationSection />;
    if (item === 'extraction') return <ExtractionTemplateSection />;
    if (item === 'smtp') return <SmtpSection />;
    if (item === 'pulse') return <PulseSection />;
    if (item === 'timer') return <TimerSection />;
    if (item === 'observability') return <div className="space-y-4"><LangfuseLinkCard /></div>;
  }

  if (section === 'integrations') {
    if (item === 'telegram') {
      return (
        <div className="space-y-8">
          <TelegramPairingSection />
          <div className="space-y-4">
            <div>
              <h3 className="text-sm font-medium">System Telegram pairing</h3>
              <p className="text-sm text-muted-foreground mt-0.5">
                Pair this JARVIS instance to a Telegram chat for system notifications
                (setup wizard / admin).
              </p>
            </div>
            <PairTelegram />
          </div>
        </div>
      );
    }
    if (item === 'zotero') return <ZoteroSection />;
  }

  if (section === 'research') {
    if (item === 'topics') return <TopicSection />;
    if (item === 'authors') return <AuthorSection />;
    if (item === 'spaced-repetition') return <SpacedRepetitionSection />;
  }

  return (
    <div className="py-12 text-center text-muted-foreground text-sm">
      Select a section from the navigation.
    </div>
  );
}

// ---------------------------------------------------------------------------
// SettingsDetailPane — public export
// ---------------------------------------------------------------------------

interface SettingsDetailPaneProps {
  section: string;
  item: string;
}

export function SettingsDetailPane({ section, item }: SettingsDetailPaneProps) {
  const sectionTitle = SECTION_TITLES[section] ?? section;
  const itemLabel =
    ITEM_LABELS[section]?.[item] ?? item.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

  return (
    <div className="flex-1 overflow-y-auto p-6 min-w-0">
      <Breadcrumb sectionTitle={sectionTitle} itemLabel={itemLabel} />
      <h2 className="font-serif text-3xl tracking-tight text-strong mb-6">{itemLabel}</h2>
      <DetailContent section={section} item={item} />
    </div>
  );
}
