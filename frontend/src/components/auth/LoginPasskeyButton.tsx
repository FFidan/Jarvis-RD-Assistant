/**
 * LoginPasskeyButton — passkey sign-in entry on the login page.
 *
 * Progressive enhancement: when passkeys are usable here it renders a real
 * sign-in button; when they are not it renders a short, mode-aware explanation
 * instead of a broken control (and nothing at all on localhost, where they work
 * and there is nothing to explain).
 */
import { Fingerprint } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { usePasskeys } from '@/hooks/usePasskeys';

/** One honest line explaining why the passkey button is absent, or null. */
function unavailableReason(
  browserSupported: boolean,
  accessMode: string | undefined,
): string | null {
  if (!browserSupported) return 'This browser does not support passkeys.';
  if (accessMode === 'lan') {
    return (
      "Passkeys work on the JARVIS computer itself, or from everywhere once you " +
      "enable the 'From anywhere' access option."
    );
  }
  // localhost is capable (handled by the caller); other modes with no server
  // capability yet get no misleading copy.
  return null;
}

export function LoginPasskeyButton() {
  const {
    capable,
    browserSupported,
    accessMode,
    capabilityLoading,
    login,
    loginPending,
    loginError,
  } = usePasskeys();

  if (!capable) {
    // Never flash the fallback copy while we are still asking the server.
    if (capabilityLoading) return null;
    const reason = unavailableReason(browserSupported, accessMode);
    return reason ? (
      <p className="text-xs text-muted-foreground text-center" role="note">
        {reason}
      </p>
    ) : null;
  }

  return (
    <div className="space-y-2">
      <Button
        type="button"
        variant="outline"
        className="w-full"
        onClick={login}
        disabled={loginPending}
        aria-busy={loginPending}
      >
        <Fingerprint className="h-4 w-4 mr-2" aria-hidden />
        {loginPending ? 'Waiting for your device…' : 'Sign in with a passkey'}
      </Button>
      <p className="text-xs text-muted-foreground text-center">
        Use your fingerprint, face, or device PIN.
      </p>
      {loginError && (
        <p className="text-sm text-destructive text-center" role="alert">
          {loginError.message}{' '}
          <button
            type="button"
            className="underline hover:no-underline"
            onClick={login}
          >
            Try again
          </button>
        </p>
      )}
    </div>
  );
}
