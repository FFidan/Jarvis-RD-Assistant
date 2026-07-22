import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, CheckCircle2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { SetupStep } from '@/components/setup/SetupStep';
import { SmtpMisconfigBanner } from '@/components/settings/SmtpMisconfigBanner';
import { getSmtpConfig, saveFirstRunSmtp, type FirstRunSmtpBody } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';
import { errorMessage } from '@/lib/errors';
import type { StepNavProps } from './shared';

interface SmtpStepProps extends StepNavProps {
  onBack: () => void;
  onNext: () => void;
  singleUser?: boolean;
  setupToken?: string | null;
}

export function SmtpStep({
  stepNumber,
  totalSteps,
  onBack,
  onNext,
  singleUser,
  setupToken,
}: SmtpStepProps) {
  const [host, setHost] = useState('');
  const [port, setPort] = useState('587');
  const [user, setUser] = useState('');
  const [password, setPassword] = useState('');
  const [fromEmail, setFromEmail] = useState('');
  const [replyTo, setReplyTo] = useState('');
  const [fromName, setFromName] = useState('');
  const [testRecipient, setTestRecipient] = useState('');
  const [savedOk, setSavedOk] = useState(false);
  const queryClient = useQueryClient();

  const smtpStatus = useQuery({
    queryKey: QUERY_KEYS.account.smtp(),
    queryFn: getSmtpConfig,
    staleTime: 60_000,
  });

  const saveMut = useMutation({
    mutationFn: (body: FirstRunSmtpBody) => saveFirstRunSmtp(body, setupToken),
    onSuccess: (res) => {
      setSavedOk(res.saved);
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.account.smtp() });
    },
  });

  const emailValid = /^\S+@\S+\.\S+$/.test(fromEmail);
  const replyToValid = replyTo === '' || /^\S+@\S+\.\S+$/.test(replyTo);
  const portNum = port === '' ? null : parseInt(port, 10);
  const portError =
    port !== '' && (Number.isNaN(portNum) || (portNum as number) < 1 || (portNum as number) > 65535)
      ? 'Port must be a number between 1 and 65535'
      : null;
  const portValid = portError === null;
  const canSave = !!host && emailValid && portValid && replyToValid;

  const buildBody = (testSend: boolean) => {
    return {
      host,
      port: portNum ?? 587,
      user: user || null,
      pass: password || null,
      from_email: fromEmail,
      reply_to: replyTo,
      from_name: fromName,
      test_send: testSend,
      test_recipient: testRecipient || null,
    };
  };

  const handleSave = (testSend: boolean) => {
    if (!canSave) return;
    saveMut.mutate(buildBody(testSend));
  };

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
          : 'Optional. Without SMTP, finish setup and copy one-time sign-in links from Admin → Users. Family members can add passkeys after signing in.'
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
      <SmtpMisconfigBanner
        deliverable={smtpStatus.data?.deliverable}
        issues={smtpStatus.data?.issues}
        className="mb-4"
      />
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
          <Input
            id="smtp-port"
            value={port}
            onChange={(e) => {
              setPort(e.target.value);
              setSavedOk(false);
            }}
            inputMode="numeric"
            aria-invalid={portError !== null}
            aria-describedby={portError ? 'smtp-port-error' : undefined}
          />
          {portError && (
            <p id="smtp-port-error" className="mt-1 text-xs text-destructive">
              {portError}
            </p>
          )}
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
          <Label htmlFor="smtp-reply-to">Reply-To address (optional)</Label>
          <Input
            id="smtp-reply-to"
            type="email"
            value={replyTo}
            onChange={(e) => {
              setReplyTo(e.target.value);
              setSavedOk(false);
            }}
            placeholder="replies-go-here@your-domain.dev"
            aria-invalid={!replyToValid}
            aria-describedby={!replyToValid ? 'smtp-reply-to-error' : undefined}
          />
          {!replyToValid && (
            <p id="smtp-reply-to-error" className="mt-1 text-xs text-destructive">
              Enter a valid email address or leave blank.
            </p>
          )}
        </div>
        <div className="sm:col-span-2">
          <Label htmlFor="smtp-from-name">Sender display name (optional)</Label>
          <Input
            id="smtp-from-name"
            value={fromName}
            onChange={(e) => {
              setFromName(e.target.value);
              setSavedOk(false);
            }}
            placeholder="JARVIS RD"
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
