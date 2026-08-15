/**
 * SettingsDetailPane — right-hand pane of the 2-pane Settings IA.
 *
 * Renders:
 *  1. Breadcrumb ("Settings / <Section title> / <item label>")
 *  2. Section heading (serif text-3xl)
 *  3. The mounted section component for the active (section, item) pair
 */
import { AIPanel } from './AIPanel';
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
import { ZoteroSection } from './ZoteroSection';
import { TopicSection } from './TopicSection';
import { AuthorSection } from './AuthorSection';
import { SourceDetailPane } from './SourceDetailPane';
import { SourcesList } from './SourcesList';
import { Link } from 'react-router-dom';
import { SmtpSection } from './SmtpSection';
import { TelegramBotTokenSection } from './TelegramBotTokenSection';
import { AccessModeSection } from './AccessModeSection';
import { SignInDevicesSection } from './SignInDevicesSection';
import { AboutSection } from './AboutSection';
import { useAuthStore } from '@/stores/auth-store';

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
    passkeys: 'Passkeys',
    appearance: 'Appearance',
  },
  sources: {
    sources: 'Sources',
  },
  models: {
    llm: 'AI models',
    providers: 'Providers & Routing',
    // Stale ?item=ai deep-links now land on the consolidated AI models page.
    ai: 'AI models',
  },
  system: {
    automation: 'Automation',
    extraction: 'Extraction Templates',
    smtp: 'Email (SMTP)',
    pulse: 'Pulse',
    timer: 'Timer',
    observability: 'Monitoring (Langfuse)',
    mode: 'Sign-in Method',
  },
  integrations: {
    telegram: 'Telegram',
    // Legacy deep link into the merged Telegram page.
    'bot-token': 'Telegram',
    zotero: 'Zotero',
  },
  research: {
    topics: 'Topics',
    authors: 'Authors',
    'spaced-repetition': 'Learning Cards',
  },
};

// ---------------------------------------------------------------------------
// Breadcrumb
// ---------------------------------------------------------------------------

function Breadcrumb({
  section,
  sectionTitle,
  itemLabel,
}: {
  section: string;
  sectionTitle: string;
  itemLabel: string;
}) {
  return (
    <nav aria-label="breadcrumb" className="flex items-center gap-1.5 text-xs text-muted-foreground mb-4">
      <Link to="/settings" className="hover:text-foreground hover:underline">Settings</Link>
      <span aria-hidden>/</span>
      <Link to={`/settings?section=${section}`} className="hover:text-foreground hover:underline">
        {sectionTitle}
      </Link>
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
  isAdmin,
  modelPickerRequest,
  providerId,
}: {
  section: string;
  item: string;
  isAdmin: boolean;
  modelPickerRequest?: {
    role: 'fast' | 'smart';
    provider: string;
  };
  providerId?: string;
}) {
  if (section === 'account') {
    if (item === 'profile') return <AccountSection />;
    if (item === 'passkeys') return <SignInDevicesSection />;
    if (item === 'appearance') return <AppearanceSection />;
  }

  if (section === 'sources') {
    // Single "Sources" item shows the full all-sources list (enable/disable/reorder/api-key).
    // Deep-links with a specific source_type slug still work via SourceDetailPane.
    if (item === 'sources' || item === '') return <SourcesList />;
    return <SourceDetailPane sourceType={item} />;
  }

  if (section === 'models') {
    // AI models is the single authoritative model plane. Backend & hardware
    // diagnostics live on the admin System Health page; this page keeps only the
    // per-role model pickers plus a compact pointer, so the two planes can't
    // drift or contradict each other.
    if (item === 'llm' || item === 'ai') {
      return (
        <div className="space-y-6">
          <IngestionSection
            filterGroups={['AI models']}
            modelPickerRequest={modelPickerRequest}
          />
          <AIPanel />
        </div>
      );
    }
    if (item === 'providers') {
      return (
        <div className="space-y-6">
          <ProvidersSection initialProviderId={providerId} />
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
    if (item === 'mode') return <AccessModeSection />;
  }

  if (section === 'integrations') {
    // One Telegram page. Pairing (every user) and the instance bot token
    // (admin) were two rail items describing one integration; they now stack
    // on a single page, and old bot-token URLs land here too.
    if (item === 'telegram' || item === 'bot-token') {
      return (
        <div className="space-y-8">
          <TelegramPairingSection />
          {isAdmin && (
            <div className="space-y-3 border-t border-hair pt-6">
              <div>
                <h3 className="text-base font-semibold">Instance bot (admin)</h3>
                <p className="text-sm text-muted-foreground">
                  The BotFather token that lets this instance send and receive Telegram
                  messages for every paired user.
                </p>
              </div>
              <TelegramBotTokenSection />
            </div>
          )}
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
  modelPickerRequest?: {
    role: 'fast' | 'smart';
    provider: string;
  };
  providerId?: string;
}

export function SettingsDetailPane({ section, item, modelPickerRequest, providerId }: SettingsDetailPaneProps) {
  const isAdmin = useAuthStore((s) => s.user)?.role === 'admin';
  const sectionTitle = SECTION_TITLES[section] ?? section;
  const itemLabel =
    ITEM_LABELS[section]?.[item] ?? item.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

  return (
    <div className="flex-1 overflow-y-auto p-6 min-w-0">
      <Breadcrumb section={section} sectionTitle={sectionTitle} itemLabel={itemLabel} />
      <h2 className="font-serif text-3xl tracking-tight text-strong mb-6">{itemLabel}</h2>
      <DetailContent
        section={section}
        item={item}
        isAdmin={isAdmin ?? false}
        modelPickerRequest={modelPickerRequest}
        providerId={providerId}
      />
      <div className="mt-8 border-t border-hair pt-6">
        <AboutSection />
      </div>
    </div>
  );
}
