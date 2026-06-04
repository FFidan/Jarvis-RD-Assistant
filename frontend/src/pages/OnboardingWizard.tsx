/**
 * Unified onboarding wizard (Task A2 — wizard consolidation).
 *
 * Replaces the two former wizards (the pre-auth FirstRunSetupPage and the
 * post-login SetupWizard) with ONE continuous flow that spans the mid-flow
 * auth boundary. App.tsx gate-renders this component (full-screen, outside
 * AppShell) whenever the pre-auth `/api/setup/status` reports the install is
 * not yet `setup_completed`.
 *
 * Step sequence (the admin-create step is conditionally skipped when an admin
 * already exists — CLI-bootstrapped or resuming):
 *
 *   1. Welcome + system check   (pre-auth)
 *   2. SMTP relay               (pre-auth)
 *   3. Create admin & sign in   (pre-auth → establishes the session)   [skippable]
 *   4. Cloud LLM keys           (post-auth)
 *   5. First research topic     (post-auth)
 *   6. Automation / Pulse       (post-auth)
 *   7. Source API keys          (post-auth)
 *   8. Pair Telegram            (post-auth)
 *   9. Done                     (post-auth → marks setup.completed)
 *
 * Displayed step numbers derive from the EFFECTIVE sequence (after the
 * conditional admin skip) so the "Step N of M" progress stays honest. The
 * URL `?step=` index is 1-based into that effective sequence.
 */
import { useEffect, useRef, useState } from 'react';
import { SystemCheck } from '@/components/setup/SystemCheck';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { UseMutationResult } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  Loader2,
  Pencil,
  RefreshCw,
  Sparkles,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { TimeSelect } from '@/components/ui/time-select';
import { SetupStep } from '@/components/setup/SetupStep';
import { TelegramPairingSection } from '@/components/settings/TelegramPairingSection';
import { getConfigValue } from '@/components/settings/pulse/pulse-utils';
import {
  createFirstRunAdmin,
  createTopic,
  fetchConfig,
  fetchSources,
  getSetupStatus,
  getTelegramPairing,
  markSetupCompleted,
  runFirstRunSystemCheck,
  saveFirstRunCloudKeys,
  saveFirstRunSmtp,
  setConfig,
  updateSource,
  type FirstRunStatus,
  type FirstRunSystemCheck,
} from '@/lib/api';
import { useAuthStore } from '@/stores/auth-store';
import { errorMessage } from '@/lib/errors';
import { timeToCron, cronToTime } from '@/lib/cron-utils';
import type { SourceConfig, Topic } from '@/types';

// Ordered identifiers for every possible step. The admin step is filtered out
// when an admin already exists; everything else is always present.
type StepKind =
  | 'welcome'
  | 'smtp'
  | 'admin'
  | 'cloud'
  | 'topic'
  | 'automation'
  | 'sources'
  | 'telegram'
  | 'done';

const ALL_STEPS: readonly StepKind[] = [
  'welcome',
  'smtp',
  'admin',
  'cloud',
  'topic',
  'automation',
  'sources',
  'telegram',
  'done',
];

interface OnboardingWizardProps {
  firstRun: FirstRunStatus;
  authed: boolean;
}

export function OnboardingWizard({ firstRun, authed }: OnboardingWizardProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // The admin-create step is only shown when no admin exists yet. When the
  // install is already `configured` (CLI-bootstrapped or resuming post-auth),
  // skip it — there is no second admin to create here.
  const showAdminStep = !firstRun.configured;
  const steps = showAdminStep ? ALL_STEPS : ALL_STEPS.filter((s) => s !== 'admin');
  const totalSteps = steps.length;

  const clampStep = (raw: string | null): number => {
    const n = raw ? parseInt(raw, 10) : 1;
    if (Number.isNaN(n) || n < 1) return 1;
    if (n > totalSteps) return totalSteps;
    return n;
  };

  const step = clampStep(searchParams.get('step'));
  const kind = steps[step - 1] ?? 'welcome';

  const goToStep = (n: number) => {
    const clamped = Math.max(1, Math.min(totalSteps, n));
    setSearchParams({ step: String(clamped) });
  };
  const goNext = () => goToStep(step + 1);
  const goBack = () => goToStep(step - 1);

  // Finishing the wizard writes the firstRun cache SYNCHRONOUSLY before
  // navigating (BUG-2 fix) — see DoneStep. handleSkipAll mirrors that path.
  const markCompletedMut = useMutation({
    mutationFn: markSetupCompleted,
    onSuccess: () => {
      markFirstRunCompleted(queryClient);
      navigate('/', { replace: true });
    },
    onError: (err: Error) => {
      console.error('Failed to mark setup completed', err);
    },
  });

  const handleSkipAll = () => {
    // Pre-auth (no session yet): "Skip setup" cannot mark setup.completed (the
    // endpoint requires the admin session). Jump to the admin-create step so
    // the operator still establishes a session before reaching the dashboard.
    if (!authed) {
      const targetKind: StepKind = showAdminStep ? 'admin' : 'cloud';
      const targetIdx = steps.indexOf(targetKind);
      goToStep(targetIdx >= 0 ? targetIdx + 1 : 1);
      return;
    }
    markCompletedMut.mutate();
  };

  const stepProps = { stepNumber: step, totalSteps };

  return (
    <div className="min-h-screen bg-background">
      {kind === 'welcome' && (
        <WelcomeSystemCheckStep
          {...stepProps}
          onNext={goNext}
          onSkip={handleSkipAll}
          skipError={markCompletedMut.isError ? markCompletedMut.error : null}
        />
      )}
      {kind === 'smtp' && (
        <SmtpStep
          {...stepProps}
          onBack={goBack}
          onNext={goNext}
          singleUser={firstRun.setup_mode === 'single'}
        />
      )}
      {kind === 'admin' && <AdminStep {...stepProps} onBack={goBack} onNext={goNext} />}
      {kind === 'cloud' && <CloudLlmStep {...stepProps} onBack={goBack} onNext={goNext} />}
      {kind === 'topic' && <FirstTopicStep {...stepProps} onBack={goBack} onNext={goNext} />}
      {kind === 'automation' && <AutomationStep {...stepProps} onBack={goBack} onNext={goNext} />}
      {kind === 'sources' && <SourceApiKeysStep {...stepProps} onBack={goBack} onNext={goNext} />}
      {kind === 'telegram' && <TelegramStep {...stepProps} onBack={goBack} onNext={goNext} />}
      {kind === 'done' && <DoneStep {...stepProps} authed={authed} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared helpers / types
// ---------------------------------------------------------------------------

interface StepNavProps {
  stepNumber: number;
  totalSteps: number;
  onBack?: () => void;
  onNext?: () => void;
}

/**
 * BUG-2 fix: write the firstRun query (the one App.tsx's gate reads) so the
 * gate re-renders with setup_completed===true and does NOT bounce back into
 * the wizard. Must run BEFORE navigate('/'). Also reconciles the post-auth
 * setup-status query.
 */
function markFirstRunCompleted(queryClient: ReturnType<typeof useQueryClient>): void {
  queryClient.setQueryData<FirstRunStatus>(QUERY_KEYS.setup.firstRun(), (prev) =>
    prev ? { ...prev, setup_completed: true } : prev,
  );
  queryClient.invalidateQueries({ queryKey: QUERY_KEYS.setup.status() });
}

// ---------------------------------------------------------------------------
// Step: Welcome + system check (merged — was two separate SystemCheck steps)
// ---------------------------------------------------------------------------

function WelcomeSystemCheckStep({
  stepNumber,
  totalSteps,
  onNext,
  onSkip,
  skipError,
}: StepNavProps & { onNext: () => void; onSkip: () => void; skipError?: Error | null }) {
  const [data, setData] = useState<FirstRunSystemCheck | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await runFirstRunSystemCheck());
    } catch (e) {
      setError(errorMessage(e, 'Probe failed'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void run();
  }, []);

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="Welcome to JARVIS"
      description="Let's make sure every backing service is reachable. The models row may still be warming up — that never blocks Continue."
      footer={
        <>
          <Button variant="ghost" onClick={onSkip}>
            Skip setup
          </Button>
          <div className="flex items-center gap-2">
            <Button variant="ghost" onClick={() => void run()} disabled={loading}>
              <RefreshCw className="mr-2 h-4 w-4" /> Re-check
            </Button>
            <Button onClick={onNext}>
              Continue <ArrowRight className="ml-2 h-4 w-4" />
            </Button>
          </div>
        </>
      }
    >
      <div className="flex items-start gap-3 rounded-md border p-4">
        <Sparkles className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
        <div className="space-y-1 text-sm">
          <p>
            JARVIS is a self-hosted research assistant: paper discovery, RAG chat over your
            library, spaced-repetition flashcards, and project tracking.
          </p>
          <p className="text-muted-foreground">
            This wizard checks your environment, creates your admin account, sets up a topic and
            the nightly Pulse schedule, and (optionally) pairs a Telegram bot.
          </p>
        </div>
      </div>
      {loading && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Probing services...
        </div>
      )}
      {error && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 text-destructive" />
          <span>{error}</span>
        </div>
      )}
      {skipError && (
        <p className="text-sm text-destructive">{errorMessage(skipError, 'Could not skip setup — try again.')}</p>
      )}
      {data && (
        <ul className="space-y-2">
          {data.services.map((svc) => (
            <li
              key={svc.name}
              className="flex items-center justify-between rounded-md border p-3 text-sm"
              data-testid={`svc-${svc.name}`}
            >
              <span className="font-medium capitalize">{svc.name}</span>
              {svc.ok ? (
                <span className="flex items-center gap-1 text-green-600">
                  <CheckCircle2 className="h-4 w-4" /> Ready
                </span>
              ) : (
                <span className="flex items-center gap-1 text-destructive" title={svc.detail ?? ''}>
                  <AlertTriangle className="h-4 w-4" /> {svc.detail ?? 'Unavailable'}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {data && !data.all_ok && (
        <p className="text-xs text-muted-foreground">
          Some services are still warming up — that's expected on first boot. You can continue and
          re-check later from Settings &rarr; System.
        </p>
      )}
    </SetupStep>
  );
}

// ---------------------------------------------------------------------------
// Step: SMTP relay
// ---------------------------------------------------------------------------

function SmtpStep({
  stepNumber,
  totalSteps,
  onBack,
  onNext,
  singleUser,
}: StepNavProps & { onBack: () => void; onNext: () => void; singleUser?: boolean }) {
  const [host, setHost] = useState('');
  const [port, setPort] = useState('587');
  const [user, setUser] = useState('');
  const [password, setPassword] = useState('');
  const [fromEmail, setFromEmail] = useState('');
  const [testRecipient, setTestRecipient] = useState('');
  const [savedOk, setSavedOk] = useState(false);

  const saveMut = useMutation({
    mutationFn: saveFirstRunSmtp,
    onSuccess: (res) => {
      setSavedOk(res.saved);
    },
  });

  // A valid SMTP config needs at least a host and a syntactically-valid
  // from-address (user/pass are optional for some relays; port defaults to 587).
  // Continue stays disabled until these hold so a half-filled form can't be
  // advanced — Skip is the intentional optional-out.
  const emailValid = /^\S+@\S+\.\S+$/.test(fromEmail);
  const canSave = !!host && emailValid;
  const buildBody = (testSend: boolean) => {
    const portNum = parseInt(port, 10);
    return {
      host,
      port: Number.isNaN(portNum) ? 587 : portNum,
      user: user || null,
      pass: password || null,
      from_email: fromEmail,
      test_send: testSend,
      test_recipient: testRecipient || null,
    };
  };

  const handleSave = (testSend: boolean) => {
    if (!canSave) return;
    saveMut.mutate(buildBody(testSend));
  };

  // FIRSTRUN-SMTP-CONTINUE-NOOP fix: Continue is only enabled once the form is
  // complete + valid (canSave), so it never advances a half-filled form. It
  // persists the config before advancing (or advances directly if the user
  // already clicked the explicit Save). To proceed WITHOUT SMTP, use Skip.
  const handleContinue = () => {
    if (savedOk) {
      onNext();
      return;
    }
    if (canSave && !saveMut.isPending) {
      saveMut.mutate(buildBody(false), { onSuccess: () => onNext() });
    }
  };

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="SMTP relay"
      description={
        singleUser
          ? "Single-user install — you log in with your API key, so email is optional. Configure SMTP only if you plan to add more users later."
          : 'Used to send magic-link login emails. Skippable — dev mode logs links to stdout instead.'
      }
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <div className="flex items-center gap-2">
            <Button variant={singleUser ? 'default' : 'ghost'} onClick={onNext}>
              {singleUser ? "Skip — I'll use my API key" : 'Skip'}
            </Button>
            <Button
              variant={singleUser ? 'ghost' : 'default'}
              onClick={handleContinue}
              disabled={saveMut.isPending || !canSave}
            >
              Continue <ArrowRight className="ml-2 h-4 w-4" />
            </Button>
          </div>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <Label htmlFor="smtp-host">Host</Label>
          <Input
            id="smtp-host"
            value={host}
            onChange={(e) => {
              setHost(e.target.value);
              setSavedOk(false);
            }}
            placeholder="smtp.resend.com"
          />
        </div>
        <div>
          <Label htmlFor="smtp-port">Port</Label>
          <Input id="smtp-port" value={port} onChange={(e) => setPort(e.target.value)} inputMode="numeric" />
        </div>
        <div>
          <Label htmlFor="smtp-user">Username</Label>
          <Input id="smtp-user" value={user} onChange={(e) => setUser(e.target.value)} placeholder="resend" autoComplete="username" />
        </div>
        <div>
          <Label htmlFor="smtp-pass">Password</Label>
          <Input id="smtp-pass" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        </div>
        <div className="sm:col-span-2">
          <Label htmlFor="smtp-from">From address</Label>
          <Input
            id="smtp-from"
            value={fromEmail}
            type="email"
            onChange={(e) => {
              setFromEmail(e.target.value);
              setSavedOk(false);
            }}
            placeholder="jarvis@your-domain.dev"
          />
        </div>
        <div className="sm:col-span-2">
          <Label htmlFor="smtp-test">Test recipient (optional)</Label>
          <Input id="smtp-test" type="email" value={testRecipient} onChange={(e) => setTestRecipient(e.target.value)} placeholder="defaults to From address" />
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={() => handleSave(false)} disabled={saveMut.isPending || !canSave}>
          Save
        </Button>
        <Button variant="outline" onClick={() => handleSave(true)} disabled={saveMut.isPending || !canSave}>
          Save & test send
        </Button>
        {saveMut.data?.saved && (
          <span className="flex items-center gap-1 text-sm text-green-600">
            <CheckCircle2 className="h-4 w-4" /> Saved
          </span>
        )}
        {savedOk && saveMut.data?.test_sent === true && <span className="text-sm text-green-600">Test email sent</span>}
        {saveMut.data?.test_sent === false && saveMut.data.test_error && (
          <span className="text-sm text-destructive" title={saveMut.data.test_error}>
            Test failed: {saveMut.data.test_error}
          </span>
        )}
      </div>
      {saveMut.isError && (
        <div className="text-sm text-destructive">Save failed: {errorMessage(saveMut.error, 'unknown error')}</div>
      )}
    </SetupStep>
  );
}

// ---------------------------------------------------------------------------
// Step: Create admin & sign in (the auth boundary). Shown only when no admin
// exists yet — establishes the browser session for the post-auth steps.
// ---------------------------------------------------------------------------

function AdminStep({
  stepNumber,
  totalSteps,
  onBack,
  onNext,
}: StepNavProps & { onBack: () => void; onNext: () => void }) {
  const [email, setEmail] = useState('');
  const loginWithSession = useAuthStore((s) => s.loginWithSession);

  const createMut = useMutation({
    mutationFn: createFirstRunAdmin,
    onSuccess: (res) => {
      // Backend has set the jarvis_session cookie atomically; mirror the
      // session into the auth store so the App flips to authed=true and the
      // post-auth wizard steps can call authenticated endpoints.
      loginWithSession({ id: res.id, email: res.email, role: res.role as 'admin' | 'user' });
      onNext();
    },
  });

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="Create your admin account"
      description="The first admin can log into the dashboard immediately — no magic-link required for this one."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button onClick={() => createMut.mutate(email)} disabled={!email || createMut.isPending}>
            {createMut.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <ArrowRight className="ml-2 h-4 w-4" />}
            Create admin & sign in
          </Button>
        </>
      }
    >
      <div>
        <Label htmlFor="admin-email">Admin email</Label>
        <Input
          id="admin-email"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="you@example.com"
          autoComplete="email"
        />
        <p className="mt-1 text-xs text-muted-foreground">
          Future admins/users go through the standard magic-link invite flow.
        </p>
      </div>
      {createMut.isError && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          {errorMessage(createMut.error, 'Could not create admin.')}
        </div>
      )}
    </SetupStep>
  );
}

// ---------------------------------------------------------------------------
// Step: Cloud LLM keys (optional)
// ---------------------------------------------------------------------------

function CloudLlmStep({
  stepNumber,
  totalSteps,
  onBack,
  onNext,
}: StepNavProps & { onBack: () => void; onNext: () => void }) {
  const [openai, setOpenai] = useState('');
  const [anthropic, setAnthropic] = useState('');
  const [gemini, setGemini] = useState('');

  const saveMut = useMutation({
    mutationFn: saveFirstRunCloudKeys,
    onSuccess: () => onNext(),
    onError: (err: Error) => {
      console.error('Failed to save cloud LLM keys', err);
    },
  });

  const anyEntered = openai || anthropic || gemini;

  function providerSaveStatus(provider: string): string | null {
    if (!saveMut.isSuccess || !saveMut.data) return null;
    const { applied_now, restart_required } = saveMut.data;
    if (applied_now.includes(provider)) return 'Applied now';
    if (restart_required) return 'Saved — applies after restart';
    return 'Saved';
  }

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="Cloud LLM keys (optional)"
      description="Skip if you only want to run on local Ollama models."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <div className="flex items-center gap-2">
            <Button variant="ghost" onClick={onNext}>
              Skip
            </Button>
            <Button
              onClick={() =>
                saveMut.mutate({ openai: openai || null, anthropic: anthropic || null, gemini: gemini || null })
              }
              disabled={!anyEntered || saveMut.isPending}
            >
              Save & continue
            </Button>
          </div>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <div className="flex items-center justify-between">
            <Label htmlFor="cloud-openai">OpenAI API key</Label>
            {openai && providerSaveStatus('openai') && (
              <span className="text-xs text-green-600">{providerSaveStatus('openai')}</span>
            )}
          </div>
          <Input id="cloud-openai" type="password" value={openai} onChange={(e) => setOpenai(e.target.value)} placeholder="sk-..." autoComplete="off" />
        </div>
        <div>
          <div className="flex items-center justify-between">
            <Label htmlFor="cloud-anthropic">Anthropic API key</Label>
            {anthropic && providerSaveStatus('anthropic') && (
              <span className="text-xs text-green-600">{providerSaveStatus('anthropic')}</span>
            )}
          </div>
          <Input id="cloud-anthropic" type="password" value={anthropic} onChange={(e) => setAnthropic(e.target.value)} placeholder="sk-ant-..." autoComplete="off" />
        </div>
        <div>
          <div className="flex items-center justify-between">
            <Label htmlFor="cloud-gemini">Google Gemini API key</Label>
            {gemini && providerSaveStatus('gemini') && (
              <span className="text-xs text-green-600">{providerSaveStatus('gemini')}</span>
            )}
          </div>
          <Input id="cloud-gemini" type="password" value={gemini} onChange={(e) => setGemini(e.target.value)} autoComplete="off" />
        </div>
      </div>
      {saveMut.isError && (
        <p className="text-sm text-destructive">{errorMessage(saveMut.error, 'Could not save keys — try again.')}</p>
      )}
    </SetupStep>
  );
}

// ---------------------------------------------------------------------------
// Step: First research topic
// ---------------------------------------------------------------------------

function FirstTopicStep({
  stepNumber,
  totalSteps,
  onBack,
  onNext,
}: StepNavProps & { onBack: () => void; onNext: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [added, setAdded] = useState(false);

  // SETUP-STEP-STATE fix: seed the "added" CTA state from the server so the
  // button reflects saved state on revisit (the user is authed by this step).
  const { data: setupStatus } = useQuery({
    queryKey: QUERY_KEYS.setup.status(),
    queryFn: getSetupStatus,
    staleTime: 30_000,
  });
  const hasTopics = (setupStatus?.topics_count ?? 0) > 0;

  const createMut = useMutation({
    mutationFn: (data: Partial<Topic>) => createTopic(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.topics.list() });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.setup.status() });
      setAdded(true);
      setName('');
      setDescription('');
    },
    onError: (err: Error) => {
      console.error('Failed to create topic', err);
    },
  });

  const handleAdd = () => {
    if (!name.trim()) return;
    createMut.mutate({
      name: name.trim(),
      query_terms: [name.trim()],
      description: description.trim() || null,
    } as Partial<Topic>);
  };

  const topicConfigured = added || hasTopics;

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="Your first research topic"
      description="Topics drive paper discovery. You can add more later in Settings."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button onClick={onNext}>
            {topicConfigured ? 'Next' : 'Skip for now'}
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <Label htmlFor="setup-topic-name">Topic name</Label>
          <Input id="setup-topic-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Neural ODEs" />
        </div>
        <div>
          <Label htmlFor="setup-topic-description">Description (optional)</Label>
          <Textarea
            id="setup-topic-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Context for the Pulse scoring LLM"
            rows={3}
            maxLength={1000}
          />
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={handleAdd} disabled={!name.trim() || createMut.isPending}>
            Add topic
          </Button>
          {topicConfigured && (
            <span className="flex items-center gap-1 text-sm text-green-600">
              <CheckCircle2 className="h-4 w-4" />
              {added ? 'Topic added' : `${setupStatus?.topics_count} topic(s) configured`}
            </span>
          )}
        </div>
        {createMut.isError && (
          <p className="text-sm text-destructive">{errorMessage(createMut.error, 'Could not add topic — try again.')}</p>
        )}
      </div>
    </SetupStep>
  );
}

// ---------------------------------------------------------------------------
// Step: Automation / Pulse schedule
// ---------------------------------------------------------------------------

function AutomationStep({
  stepNumber,
  totalSteps,
  onBack,
  onNext,
}: StepNavProps & { onBack: () => void; onNext: () => void }) {
  const queryClient = useQueryClient();

  // SETUP-STEP-STATE fix: seed the form + "saved" badge from persisted config
  // so a revisit reflects what's actually stored, not the defaults.
  const { data: configs } = useQuery({
    queryKey: QUERY_KEYS.config.all(),
    queryFn: fetchConfig,
    staleTime: 30_000,
  });
  const persistedCron = configs ? getConfigValue<string | null>(configs, 'pulse.cron', null) : null;
  const persistedEnabled = configs ? getConfigValue<boolean | null>(configs, 'pulse.enabled', null) : null;

  const [time, setTime] = useState('04:00');
  const [pulseEnabled, setPulseEnabled] = useState(true);
  const [saved, setSaved] = useState(false);
  // Hydrate local state once the persisted config arrives (config is the source
  // of truth on revisit). `seeded` guards against clobbering user edits on
  // subsequent config refetches.
  const seededRef = useRef(false);
  useEffect(() => {
    if (seededRef.current || !configs) return;
    seededRef.current = true;
    if (persistedCron) setTime(cronToTime(persistedCron));
    if (persistedEnabled !== null) setPulseEnabled(persistedEnabled);
    if (persistedCron !== null || persistedEnabled !== null) setSaved(true);
  }, [configs, persistedCron, persistedEnabled]);

  const saveMut = useMutation({
    mutationFn: async (value: string) => {
      await setConfig('pulse.cron', value);
      await setConfig('pulse.enabled', pulseEnabled);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.config.all() });
      setSaved(true);
    },
    onError: (err: Error) => {
      console.error('Failed to save pulse config', err);
    },
  });

  const handleSave = () => {
    saveMut.mutate(timeToCron(time));
  };

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="Automation schedule"
      description="Pulse will run daily at this time to discover new papers."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button onClick={onNext}>
            {saved ? 'Next' : 'Skip for now'}
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={pulseEnabled}
            onChange={(e) => setPulseEnabled(e.target.checked)}
            className="h-4 w-4 rounded border-gray-300"
            aria-label="Enable Pulse"
          />
          <span className="text-sm font-medium">Enable Pulse (overnight paper discovery)</span>
        </label>
        <div>
          <Label htmlFor="setup-pulse-time">Daily run time</Label>
          <TimeSelect value={time} onChange={setTime} disabled={!pulseEnabled} />
          <p className="mt-1 text-xs text-muted-foreground">
            Equivalent cron: <span className="font-mono">{timeToCron(time)}</span>
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={handleSave} disabled={saveMut.isPending}>
            Save schedule
          </Button>
          {saved && (
            <span className="flex items-center gap-1 text-sm text-green-600">
              <CheckCircle2 className="h-4 w-4" />
              Saved
            </span>
          )}
        </div>
        {saveMut.isError && (
          <p className="text-sm text-destructive">{errorMessage(saveMut.error, 'Could not save schedule — try again.')}</p>
        )}
      </div>
    </SetupStep>
  );
}

// ---------------------------------------------------------------------------
// Step: Source API keys (inlined — the standalone SourceApiKeysStep component
// hardcodes its own step numbering, so it cannot be reused in the unified
// progress bar). Logic mirrors components/setup/SourceApiKeysStep.tsx.
// ---------------------------------------------------------------------------

const KEY_SOURCES = ['semantic_scholar', 'openalex', 'pubmed'] as const;
const SOURCE_LABELS: Record<string, { name: string; description: string }> = {
  semantic_scholar: { name: 'Semantic Scholar', description: 'Academic paper search (recommended)' },
  openalex: { name: 'OpenAlex', description: 'Open academic graph' },
  pubmed: { name: 'PubMed', description: 'Biomedical literature (NCBI)' },
};

function SourceKeyRow({ source }: { source: SourceConfig }) {
  const [editing, setEditing] = useState(false);
  const [apiKey, setApiKey] = useState('');
  const queryClient = useQueryClient();
  const meta = SOURCE_LABELS[source.source_type];

  const saveMut = useMutation({
    mutationFn: () => updateSource(source.id, { config: { ...(source.config ?? {}), api_key: apiKey } }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.sources.list() });
      setEditing(false);
      setApiKey('');
    },
    onError: (err: Error) => {
      console.error('Failed to save source key', err);
    },
  });

  const hasKey = !!(source.config as Record<string, unknown>)?.api_key;

  return (
    <div className="rounded-md border p-3 space-y-2">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium">{meta?.name ?? source.source_type}</p>
          <p className="text-xs text-muted-foreground">{meta?.description}</p>
        </div>
        {!editing && (
          <Button variant="ghost" size="sm" onClick={() => setEditing(true)}>
            <Pencil className="h-3.5 w-3.5 mr-1" />
            {hasKey ? 'Edit key' : 'Add key'}
          </Button>
        )}
      </div>
      {editing && (
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <Input
              type="password"
              placeholder="Paste API key…"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              className="h-8 text-sm flex-1"
              autoFocus
            />
            <Button size="sm" variant="ghost" disabled={!apiKey || saveMut.isPending} onClick={() => saveMut.mutate()}>
              <Check className="h-4 w-4" />
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setEditing(false);
                setApiKey('');
              }}
            >
              <X className="h-4 w-4" />
            </Button>
          </div>
          {saveMut.isError && (
            <span className="text-xs text-destructive">{errorMessage(saveMut.error, 'Save failed')}</span>
          )}
        </div>
      )}
      {hasKey && !editing && <p className="text-xs text-green-600">API key configured</p>}
    </div>
  );
}

function SourceApiKeysStep({
  stepNumber,
  totalSteps,
  onBack,
  onNext,
}: StepNavProps & { onBack: () => void; onNext: () => void }) {
  const { data: sources, isLoading } = useQuery({
    queryKey: QUERY_KEYS.sources.list(),
    queryFn: fetchSources,
  });

  const keyedSources = sources?.filter((s) =>
    KEY_SOURCES.includes(s.source_type as (typeof KEY_SOURCES)[number]),
  );

  const anyKeyConfigured = keyedSources?.some((s) => !!(s.config as Record<string, unknown>)?.api_key);

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="Configure API Keys"
      description="Optional — sources work without keys, but adding them increases rate limits for paper discovery."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button onClick={onNext}>
            {anyKeyConfigured ? 'Next' : 'Skip for now'}
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading sources…</p>
        ) : (
          keyedSources?.map((source) => <SourceKeyRow key={source.id} source={source} />)
        )}
        <p className="text-xs text-muted-foreground">You can update these later in Settings → Sources.</p>
      </div>
    </SetupStep>
  );
}

// ---------------------------------------------------------------------------
// Step: Pair Telegram (optional)
// ---------------------------------------------------------------------------

function TelegramStep({
  stepNumber,
  totalSteps,
  onBack,
  onNext,
}: StepNavProps & { onBack: () => void; onNext: () => void }) {
  // BUG-1 fix: read the pairing query directly (fresh) rather than the 30s-stale
  // SetupStatus.telegram_paired, so the button reflects pairing as soon as it
  // completes within this step.
  const { data } = useQuery({
    queryKey: QUERY_KEYS.pairing.userTelegram(),
    queryFn: getTelegramPairing,
  });
  const paired = data?.paired === true;

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="Pair Telegram (optional)"
      description="Receive daily briefings and interact with JARVIS from your phone."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button onClick={onNext}>
            {paired ? 'Next' : 'Skip for now'}
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
      <TelegramPairingSection />
    </SetupStep>
  );
}

// ---------------------------------------------------------------------------
// Step: Done — marks setup.completed and navigates home.
// ---------------------------------------------------------------------------

function DoneStep({
  stepNumber,
  totalSteps,
  authed,
}: {
  stepNumber: number;
  totalSteps: number;
  authed: boolean;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const hasTriggered = useRef(false);

  const markMut: UseMutationResult<void, Error, void> = useMutation({
    mutationFn: markSetupCompleted,
    onSuccess: () => {
      // BUG-2 fix: write the firstRun cache the gate reads BEFORE navigating so
      // the gate sees setup_completed===true and does not bounce back here.
      markFirstRunCompleted(queryClient);
      navigate('/', { replace: true });
    },
    onError: (err: Error) => {
      console.error('Failed to mark setup completed', err);
    },
  });

  useEffect(() => {
    if (!hasTriggered.current) {
      hasTriggered.current = true;
      markMut.mutate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (markMut.isError) {
    return (
      // BUG-3 fix: the error branch must carry the SAME step number as the
      // success branch (the real final step) — not a hardcoded earlier value.
      <SetupStep
        stepNumber={stepNumber}
        totalSteps={totalSteps}
        title="You're all set"
        description="JARVIS is ready to help with your research."
        footer={<span />}
      >
        <div className="space-y-4">
          <div className="flex items-start gap-3 rounded-md border border-destructive/40 bg-destructive/10 p-4">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
            <div className="space-y-1 text-sm">
              <p className="font-medium">Setup completion failed</p>
              <p className="text-muted-foreground">{errorMessage(markMut.error, 'Could not save setup status.')}</p>
            </div>
          </div>
          <Button
            onClick={() => {
              hasTriggered.current = false;
              markMut.reset();
              markMut.mutate();
              hasTriggered.current = true;
            }}
          >
            <RefreshCw className="mr-2 h-4 w-4" />
            Retry
          </Button>
        </div>
      </SetupStep>
    );
  }

  return (
    <SetupStep
      stepNumber={stepNumber}
      totalSteps={totalSteps}
      title="You're all set"
      description="JARVIS is ready to help with your research."
      footer={
        <>
          <span />
          {markMut.isPending ? (
            <Button disabled>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              Finishing…
            </Button>
          ) : (
            <Button
              onClick={() => {
                markFirstRunCompleted(queryClient);
                navigate('/', { replace: true });
              }}
            >
              Go to dashboard
              <ArrowRight className="ml-2 h-4 w-4" />
            </Button>
          )}
        </>
      }
    >
      <div className="flex items-start gap-3 rounded-md border border-green-500/40 bg-green-500/10 p-4">
        <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-green-500" />
        <div className="space-y-1 text-sm">
          <p className="font-medium">Setup complete</p>
          <p className="text-muted-foreground">
            You can revisit any of these settings from the Settings page. Integrations like Telegram
            live under Settings &rarr; Integrations.
          </p>
        </div>
      </div>
      {/* Setup readiness — SystemCheck polls the auth-gated /api/system/setup-status
          (returns 403 pre-auth). The Done step is always reached post-auth, but guard
          explicitly so a future refactor can't accidentally call the endpoint unauthenticated. */}
      {authed && (
        <div className="space-y-2">
          <p className="text-sm font-medium text-muted-foreground">Setup readiness</p>
          <SystemCheck />
        </div>
      )}
    </SetupStep>
  );
}

export default OnboardingWizard;
