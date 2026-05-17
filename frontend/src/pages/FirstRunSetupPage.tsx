/**
 * Phase 2 WS-2F first-run wizard (pre-auth).
 *
 * This wizard runs BEFORE any user is authenticated. It bootstraps an
 * unconfigured install through:
 *
 *   1. Welcome + system check (Postgres / Qdrant / Ollama / LiteLLM probe).
 *   2. SMTP config (host/port/user/pass/from + optional test send). Skippable.
 *   3. Admin email (creates the first admin user AND the browser session
 *      atomically — no magic-link round-trip; the operator is right here).
 *   4. Cloud LLM provider keys (optional, skippable).
 *   5. Done — auto-redirect to '/'.
 *
 * NOTE on naming: the legacy post-login onboarding wizard already owns the
 * /setup route (frontend/src/pages/SetupWizard.tsx). This pre-auth wizard
 * therefore lives at /first-run. App.tsx redirects unconfigured installs
 * here from any route.
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, ArrowRight, CheckCircle2, Loader2, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { SetupStep } from '@/components/setup/SetupStep';
import {
  createFirstRunAdmin,
  getFirstRunStatus,
  runFirstRunSystemCheck,
  saveFirstRunCloudKeys,
  saveFirstRunSmtp,
  type FirstRunSystemCheck,
} from '@/lib/api';
import { useAuthStore } from '@/stores/auth-store';

const TOTAL_STEPS = 5;

export function FirstRunSetupPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [step, setStep] = useState(1);
  const [adminEmail, setAdminEmail] = useState<string | null>(null);

  // If the install is already configured (somebody else just finished the
  // wizard, or the operator hand-bootstrapped a user), bounce to '/'. Avoids
  // showing a stale wizard.
  const { data: status } = useQuery({
    queryKey: ['first-run-status'],
    queryFn: getFirstRunStatus,
    staleTime: 0,
    retry: false,
  });
  useEffect(() => {
    if (status?.configured && step === 1) {
      navigate('/', { replace: true });
    }
  }, [status?.configured, navigate, step]);

  const goNext = () => setStep((s) => Math.min(TOTAL_STEPS, s + 1));
  const goBack = () => setStep((s) => Math.max(1, s - 1));
  const finish = () => {
    queryClient.invalidateQueries({ queryKey: ['first-run-status'] });
    navigate('/', { replace: true });
  };

  return (
    <div className="min-h-screen bg-background">
      {step === 1 && <SystemCheckStep onNext={goNext} />}
      {step === 2 && <SmtpStep onBack={goBack} onNext={goNext} singleUser={status?.setup_mode === 'single'} />}
      {step === 3 && (
        <AdminStep
          onBack={goBack}
          onNext={(email) => {
            setAdminEmail(email);
            goNext();
          }}
        />
      )}
      {step === 4 && <CloudLlmStep onBack={goBack} onNext={goNext} />}
      {step === 5 && <DoneStep adminEmail={adminEmail} onFinish={finish} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 1: System check
// ---------------------------------------------------------------------------

function SystemCheckStep({ onNext }: { onNext: () => void }) {
  const [data, setData] = useState<FirstRunSystemCheck | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await runFirstRunSystemCheck();
      setData(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Probe failed');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <SetupStep
      stepNumber={1}
      totalSteps={TOTAL_STEPS}
      title="Welcome to JARVIS"
      description="Let's make sure every backing service is reachable."
      footer={
        <>
          <Button variant="ghost" onClick={() => void run()} disabled={loading}>
            <RefreshCw className="mr-2 h-4 w-4" /> Re-check
          </Button>
          <Button onClick={onNext}>
            Continue <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
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
          Some services are still warming up — that's expected on first boot. You can continue
          and re-check later from Settings &rarr; System.
        </p>
      )}
    </SetupStep>
  );
}

// ---------------------------------------------------------------------------
// Step 2: SMTP
// ---------------------------------------------------------------------------

function SmtpStep({ onBack, onNext, singleUser }: { onBack: () => void; onNext: () => void; singleUser?: boolean }) {
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

  const handleSave = (testSend: boolean) => {
    if (!host || !fromEmail) return;
    const portNum = parseInt(port, 10);
    if (Number.isNaN(portNum)) return;
    saveMut.mutate({
      host,
      port: portNum,
      user: user || null,
      pass: password || null,
      from_email: fromEmail,
      test_send: testSend,
      test_recipient: testRecipient || null,
    });
  };

  return (
    <SetupStep
      stepNumber={2}
      totalSteps={TOTAL_STEPS}
      title="SMTP relay"
      description={singleUser
        ? "Single-user install — you log in with your API key, so email is optional. Configure SMTP only if you plan to add more users later."
        : "Used to send magic-link login emails. Skippable — dev mode logs links to stdout instead."}
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <div className="flex items-center gap-2">
            <Button variant={singleUser ? 'default' : 'ghost'} onClick={onNext}>
              {singleUser ? 'Skip — I\'ll use my API key' : 'Skip'}
            </Button>
            <Button variant={singleUser ? 'ghost' : 'default'} onClick={onNext} disabled={!singleUser && saveMut.isPending}>
              Continue <ArrowRight className="ml-2 h-4 w-4" />
            </Button>
          </div>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <Label htmlFor="smtp-host">Host</Label>
          <Input id="smtp-host" value={host} onChange={(e) => setHost(e.target.value)}
                 placeholder="smtp.resend.com" />
        </div>
        <div>
          <Label htmlFor="smtp-port">Port</Label>
          <Input id="smtp-port" value={port} onChange={(e) => setPort(e.target.value)} inputMode="numeric" />
        </div>
        <div>
          <Label htmlFor="smtp-user">Username</Label>
          <Input id="smtp-user" value={user} onChange={(e) => setUser(e.target.value)}
                 placeholder="resend" autoComplete="username" />
        </div>
        <div>
          <Label htmlFor="smtp-pass">Password</Label>
          <Input id="smtp-pass" type="password" value={password}
                 onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        </div>
        <div className="sm:col-span-2">
          <Label htmlFor="smtp-from">From address</Label>
          <Input id="smtp-from" value={fromEmail} type="email"
                 onChange={(e) => setFromEmail(e.target.value)} placeholder="jarvis@your-domain.dev" />
        </div>
        <div className="sm:col-span-2">
          <Label htmlFor="smtp-test">Test recipient (optional)</Label>
          <Input id="smtp-test" type="email" value={testRecipient}
                 onChange={(e) => setTestRecipient(e.target.value)}
                 placeholder="defaults to From address" />
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={() => handleSave(false)} disabled={saveMut.isPending || !host || !fromEmail}>
          Save
        </Button>
        <Button variant="outline" onClick={() => handleSave(true)}
                disabled={saveMut.isPending || !host || !fromEmail}>
          Save & test send
        </Button>
        {saveMut.data?.saved && (
          <span className="flex items-center gap-1 text-sm text-green-600">
            <CheckCircle2 className="h-4 w-4" /> Saved
          </span>
        )}
        {savedOk && saveMut.data?.test_sent === true && (
          <span className="text-sm text-green-600">Test email sent</span>
        )}
        {saveMut.data?.test_sent === false && saveMut.data.test_error && (
          <span className="text-sm text-destructive" title={saveMut.data.test_error}>
            Test failed: {saveMut.data.test_error}
          </span>
        )}
      </div>
      {saveMut.isError && (
        <div className="text-sm text-destructive">
          Save failed: {saveMut.error instanceof Error ? saveMut.error.message : 'unknown error'}
        </div>
      )}
    </SetupStep>
  );
}

// ---------------------------------------------------------------------------
// Step 3: Admin email — creates first admin + session
// ---------------------------------------------------------------------------

function AdminStep({
  onBack,
  onNext,
}: {
  onBack: () => void;
  onNext: (email: string) => void;
}) {
  const [email, setEmail] = useState('');
  const loginWithSession = useAuthStore((s) => s.loginWithSession);

  const createMut = useMutation({
    mutationFn: createFirstRunAdmin,
    onSuccess: (res) => {
      // Backend has set the jarvis_session cookie atomically; mirror the
      // session into the auth store so the UI flips to authed=true and the
      // FirstRunGate stops redirecting once configured=true is observed.
      loginWithSession({ id: res.id, email: res.email, role: res.role as 'admin' | 'user' });
      onNext(res.email);
    },
  });

  return (
    <SetupStep
      stepNumber={3}
      totalSteps={TOTAL_STEPS}
      title="Create your admin account"
      description="The first admin can log into the dashboard immediately — no magic-link required for this one."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button
            onClick={() => createMut.mutate(email)}
            disabled={!email || createMut.isPending}
          >
            {createMut.isPending ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <ArrowRight className="ml-2 h-4 w-4" />
            )}
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
          {createMut.error instanceof Error ? createMut.error.message : 'Could not create admin.'}
        </div>
      )}
    </SetupStep>
  );
}

// ---------------------------------------------------------------------------
// Step 4: Cloud LLM keys (optional)
// ---------------------------------------------------------------------------

function CloudLlmStep({ onBack, onNext }: { onBack: () => void; onNext: () => void }) {
  const [openai, setOpenai] = useState('');
  const [anthropic, setAnthropic] = useState('');
  const [gemini, setGemini] = useState('');

  const saveMut = useMutation({
    mutationFn: saveFirstRunCloudKeys,
    onSuccess: () => onNext(),
  });

  const anyEntered = openai || anthropic || gemini;

  // Build a per-provider inline status label from the mutation response.
  function providerSaveStatus(provider: string): string | null {
    if (!saveMut.isSuccess || !saveMut.data) return null;
    const { applied_now, restart_required } = saveMut.data;
    if (applied_now.includes(provider)) return 'Applied now';
    if (restart_required) return 'Saved — applies after restart';
    return 'Saved';
  }

  return (
    <SetupStep
      stepNumber={4}
      totalSteps={TOTAL_STEPS}
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
                saveMut.mutate({
                  openai: openai || null,
                  anthropic: anthropic || null,
                  gemini: gemini || null,
                })
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
          <Input
            id="cloud-openai"
            type="password"
            value={openai}
            onChange={(e) => setOpenai(e.target.value)}
            placeholder="sk-..."
            autoComplete="off"
          />
        </div>
        <div>
          <div className="flex items-center justify-between">
            <Label htmlFor="cloud-anthropic">Anthropic API key</Label>
            {anthropic && providerSaveStatus('anthropic') && (
              <span className="text-xs text-green-600">{providerSaveStatus('anthropic')}</span>
            )}
          </div>
          <Input
            id="cloud-anthropic"
            type="password"
            value={anthropic}
            onChange={(e) => setAnthropic(e.target.value)}
            placeholder="sk-ant-..."
            autoComplete="off"
          />
        </div>
        <div>
          <div className="flex items-center justify-between">
            <Label htmlFor="cloud-gemini">Google Gemini API key</Label>
            {gemini && providerSaveStatus('gemini') && (
              <span className="text-xs text-green-600">{providerSaveStatus('gemini')}</span>
            )}
          </div>
          <Input
            id="cloud-gemini"
            type="password"
            value={gemini}
            onChange={(e) => setGemini(e.target.value)}
            autoComplete="off"
          />
        </div>
      </div>
    </SetupStep>
  );
}

// ---------------------------------------------------------------------------
// Step 5: Done
// ---------------------------------------------------------------------------

function DoneStep({ adminEmail, onFinish }: { adminEmail: string | null; onFinish: () => void }) {
  // Auto-redirect after a short hold so the user sees the confirmation.
  useEffect(() => {
    const t = setTimeout(onFinish, 1500);
    return () => clearTimeout(t);
  }, [onFinish]);

  return (
    <SetupStep
      stepNumber={5}
      totalSteps={TOTAL_STEPS}
      title="You're all set"
      description="Loading the dashboard..."
      footer={
        <>
          <span />
          <Button onClick={onFinish}>
            Open dashboard <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
      <div className="flex items-start gap-3 rounded-md border border-green-500/40 bg-green-500/10 p-4">
        <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-green-500" />
        <div className="space-y-1 text-sm">
          <p className="font-medium">JARVIS is configured.</p>
          {adminEmail && (
            <p className="text-muted-foreground">
              Signed in as <span className="font-medium">{adminEmail}</span> (admin role).
            </p>
          )}
          <p className="text-muted-foreground">
            Invite teammates from Settings &rarr; Admin &rarr; Users.
          </p>
        </div>
      </div>
    </SetupStep>
  );
}

export default FirstRunSetupPage;
