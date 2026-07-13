/**
 * SignInDevicesSection — Settings → Account → Passkeys.
 *
 * Lets a signed-in user register a passkey on the current device, see the ones
 * they already have (added / last-used), and revoke any of them. Revoking the
 * last passkey is called out because it drops the user back to magic-link /
 * API-key sign-in. The register/revoke ceremonies live in `usePasskeys`.
 */
import { useEffect, useState } from 'react';
import { formatDistanceToNow } from 'date-fns';
import { toast } from 'sonner';
import { Fingerprint, KeyRound, Plus, Trash2 } from 'lucide-react';
import { usePasskeys, type PasskeyError } from '@/hooks/usePasskeys';
import type { PasskeyInfo } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';

function formatWhen(iso: string | null): string {
  if (!iso) return 'Never';
  try {
    return formatDistanceToNow(new Date(iso), { addSuffix: true });
  } catch {
    return iso;
  }
}

/** Best-effort "Browser on OS" label so a new passkey has a recognisable name. */
export function defaultPasskeyNickname(userAgent: string = navigator.userAgent): string {
  const os = /Windows/.test(userAgent)
    ? 'Windows'
    : /Android/.test(userAgent)
      ? 'Android'
      : /iPhone|iPad|iPod/.test(userAgent)
        ? 'iOS'
        : /Mac OS X|Macintosh/.test(userAgent)
          ? 'macOS'
          : /Linux/.test(userAgent)
            ? 'Linux'
            : 'this device';
  const browser = /Edg\//.test(userAgent)
    ? 'Edge'
    : /Firefox\//.test(userAgent)
      ? 'Firefox'
      : /Chrome\//.test(userAgent)
        ? 'Chrome'
        : /Safari\//.test(userAgent)
          ? 'Safari'
          : 'browser';
  return `${browser} on ${os}`;
}

// ---------------------------------------------------------------------------
// Register dialog (presentational — the ceremony lives in the parent's hook)
// ---------------------------------------------------------------------------

interface RegisterPasskeyDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRegister: (nickname?: string) => Promise<unknown>;
  pending: boolean;
  error: PasskeyError | null;
  resetError: () => void;
}

function RegisterPasskeyDialog({
  open,
  onOpenChange,
  onRegister,
  pending,
  error,
  resetError,
}: RegisterPasskeyDialogProps) {
  const [nickname, setNickname] = useState('');

  useEffect(() => {
    if (open) {
      setNickname(defaultPasskeyNickname());
      resetError();
    }
  }, [open, resetError]);

  async function handleRegister() {
    try {
      await onRegister(nickname.trim() || undefined);
      toast.success('Passkey added.');
      onOpenChange(false);
    } catch {
      // `error` already carries the typed message for the alert below.
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !(pending && !next) && onOpenChange(next)}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add a passkey</DialogTitle>
          <DialogDescription>
            Your device will ask for your fingerprint, face, or PIN to create a passkey.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="passkey-nickname">Name (optional)</Label>
            <Input
              id="passkey-nickname"
              value={nickname}
              onChange={(e) => setNickname(e.target.value)}
              placeholder="e.g. Chrome on macOS"
              autoFocus
              disabled={pending}
            />
          </div>
          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error.message}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={pending}>
            Cancel
          </Button>
          <Button type="button" onClick={handleRegister} disabled={pending} aria-busy={pending}>
            {pending ? 'Waiting for your device…' : 'Create passkey'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Section
// ---------------------------------------------------------------------------

export function SignInDevicesSection() {
  const {
    capable,
    browserSupported,
    accessMode,
    passkeys,
    passkeysLoading,
    passkeysError,
    registerPasskey,
    registerPending,
    registerError,
    resetRegisterError,
    deletePasskey,
    deletePending,
    deletingId,
  } = usePasskeys({ includeList: true });

  const [registerOpen, setRegisterOpen] = useState(false);
  const [pendingRevoke, setPendingRevoke] = useState<PasskeyInfo | null>(null);

  const list = passkeys ?? [];
  const isLastPasskey = list.length === 1;

  async function handleRevoke() {
    if (!pendingRevoke) return;
    const target = pendingRevoke;
    setPendingRevoke(null);
    try {
      await deletePasskey(target.id);
      toast.success('Passkey removed.');
    } catch {
      toast.error("Couldn't remove that passkey. Please try again.");
    }
  }

  if (!capable) {
    const reason = !browserSupported
      ? 'This browser does not support passkeys.'
      : accessMode === 'lan'
        ? "Passkeys work on the JARVIS computer itself, or from everywhere once you enable the 'From anywhere' access option."
        : 'Passkeys are not available with the current access mode.';
    return (
      <Card className="rounded-md border-hair shadow-none">
        <CardContent className="py-6">
          <p className="text-sm text-muted-foreground" role="note">
            {reason}
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
        <p className="text-sm text-muted-foreground">
          Passkeys let you sign in with your fingerprint, face, or device PIN instead of
          an email link. Add one per device you use.
        </p>
        <Button type="button" size="sm" onClick={() => setRegisterOpen(true)} className="shrink-0">
          <Plus className="h-4 w-4 mr-1.5" aria-hidden />
          Add a passkey
        </Button>
      </CardHeader>

      <CardContent>
        {passkeysLoading ? (
          <p className="text-sm text-muted-foreground">Loading your passkeys…</p>
        ) : passkeysError ? (
          <p className="text-sm text-destructive" role="alert">
            Couldn&apos;t load your passkeys. Please refresh.
          </p>
        ) : list.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-8 text-center">
            <KeyRound className="h-6 w-6 text-muted-foreground" aria-hidden />
            <p className="text-sm text-muted-foreground">
              No passkeys yet. Add one to sign in with your fingerprint, face, or device PIN.
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-hair">
            {list.map((passkey) => (
              <li key={passkey.id} className="flex items-center gap-3 py-3">
                <Fingerprint className="h-5 w-5 text-muted-foreground shrink-0" aria-hidden />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium truncate">{passkey.nickname}</p>
                  <p className="text-xs text-muted-foreground">
                    Added {formatWhen(passkey.created_at)} · Last used {formatWhen(passkey.last_used_at)}
                  </p>
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-muted-foreground hover:text-destructive"
                  disabled={deletePending && deletingId === passkey.id}
                  onClick={() => setPendingRevoke(passkey)}
                  aria-label={`Remove passkey ${passkey.nickname}`}
                  title={`Remove passkey ${passkey.nickname}`}
                >
                  <Trash2 className="h-4 w-4" aria-hidden />
                </Button>
              </li>
            ))}
          </ul>
        )}
      </CardContent>

      <RegisterPasskeyDialog
        open={registerOpen}
        onOpenChange={setRegisterOpen}
        onRegister={registerPasskey}
        pending={registerPending}
        error={registerError}
        resetError={resetRegisterError}
      />

      <AlertDialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => !open && setPendingRevoke(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remove this passkey?</AlertDialogTitle>
            <AlertDialogDescription>
              {isLastPasskey ? (
                <>
                  This is your only passkey. After removing it you&apos;ll sign in with a
                  magic link or API key until you add a new one.
                </>
              ) : (
                <>
                  You won&apos;t be able to sign in with{' '}
                  <strong>{pendingRevoke?.nickname}</strong> anymore. Your other passkeys keep working.
                </>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setPendingRevoke(null)}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleRevoke}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Remove
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}
