import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { ArrowRight, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { SetupStep } from '@/components/setup/SetupStep';
import { createFirstRunAdmin } from '@/lib/api';
import { ApiError } from '@/lib/api/core';
import { useAuthStore } from '@/stores/auth-store';
import { errorMessage } from '@/lib/errors';
import { storeSetupToken } from './shared';
import type { StepNavProps } from './shared';

interface AdminStepProps extends StepNavProps {
  onBack: () => void;
  onNext: () => void;
  setupToken?: string | null;
}

export function AdminStep({
  stepNumber,
  totalSteps,
  onBack,
  onNext,
  setupToken,
}: AdminStepProps) {
  const [email, setEmail] = useState('');
  const [pastedToken, setPastedToken] = useState('');
  const loginWithSession = useAuthStore((s) => s.loginWithSession);

  const effectiveToken = pastedToken.trim() || setupToken;

  const createMut = useMutation({
    mutationFn: (adminEmail: string) => createFirstRunAdmin(adminEmail, effectiveToken),
    onSuccess: async (res) => {
      if (pastedToken.trim()) storeSetupToken(pastedToken.trim());
      await loginWithSession({ id: res.id, email: res.email, role: res.role as 'admin' | 'user' });
      onNext();
    },
  });

  // A 403 while this browser carries no token means the server has one
  // configured that we never received (second device / incognito) — offer a
  // paste field so the operator can enter the line printed by ./setup.sh.
  const needsSetupToken =
    !setupToken &&
    createMut.error instanceof ApiError &&
    createMut.error.status === 403 &&
    /token/i.test(createMut.error.detail);

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
          <Button
            onClick={() => createMut.mutate(email)}
            disabled={!email || createMut.isPending || (needsSetupToken && !pastedToken.trim())}
          >
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
          Add family members later from Admin → Users. You can copy a one-time sign-in link
          to them without email; after signing in, they can add a passkey for future logins.
        </p>
      </div>
      {needsSetupToken && (
        <div>
          <Label htmlFor="setup-token">Setup token</Label>
          <Input
            id="setup-token"
            type="text"
            value={pastedToken}
            onChange={(e) => setPastedToken(e.target.value)}
            placeholder="paste the setup token"
            autoComplete="off"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            Printed by ./setup.sh on the server — the line ending in /setup#setup_token=…
          </p>
        </div>
      )}
      {createMut.isError && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          {errorMessage(createMut.error, 'Could not create admin.')}
        </div>
      )}
    </SetupStep>
  );
}
