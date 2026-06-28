import { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import type { SessionUser } from '@/stores/auth-store';
import { useAuthStore } from '@/stores/auth-store';
import { verifyMagicLink } from '@/lib/api';
import { errorMessage } from '@/lib/errors';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

/**
 * Handles the magic-link landing URL.
 *
 * URL shape: /auth/verify?token=<urlsafe-32>
 *
 * Behavior:
 * - On mount, POST the token to /api/auth/verify.
 * - On success, the backend sets the jarvis_session HttpOnly cookie and
 *   returns the user record. We push the user record into auth-store
 *   (loginWithSession) and navigate to "/".
 * - On failure, navigate to /login?error=<reason>.
 *
 * StrictMode-safety: React 18 double-invokes effects in dev. The module-level
 * Map dedupes the single-use token POST so both mounts share one in-flight
 * promise — the live mount navigates on success; no double-POST, no spurious
 * rejection from a consumed token.
 */

// Dedupe the single-use verify across StrictMode's dev double-mount so the
// token is POSTed exactly once; both mounts await the same promise.
const inflightVerifications = new Map<string, Promise<SessionUser>>();

function verifyOnce(token: string): Promise<SessionUser> {
  let p = inflightVerifications.get(token);
  if (!p) {
    p = verifyMagicLink(token);
    inflightVerifications.set(token, p);
  }
  return p;
}

/** Exposed only for test tear-down — call in beforeEach to prevent Map leakage across tests. */
export function __resetVerifyDedupeForTests(): void {
  inflightVerifications.clear();
}

export function AuthVerifyPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { loginWithSession } = useAuthStore();
  const [status, setStatus] = useState<'verifying' | 'error'>('verifying');
  const [errorMsg, setErrorMsg] = useState('');
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const token = searchParams.get('token');
    if (!token) {
      navigate('/login?error=Missing+token', { replace: true });
      return;
    }

    let cancelled = false;
    void (async () => {
      try {
        const user = await verifyOnce(token);
        if (cancelled) return;
        await loginWithSession(user);
        if (cancelled) return;
        navigate('/', { replace: true });
      } catch (err) {
        if (cancelled) return;
        const message = errorMessage(err, 'Invalid or expired link');
        setErrorMsg(message);
        setStatus('error');
        timerRef.current = setTimeout(() => {
          navigate(`/login?error=${encodeURIComponent(message)}`, { replace: true });
        }, 2000);
      }
    })();

    return () => {
      cancelled = true;
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <CardTitle className="text-xl">
            {status === 'verifying' ? 'Signing you in...' : 'Sign-in failed'}
          </CardTitle>
        </CardHeader>
        <CardContent className="text-center text-sm text-muted-foreground">
          {status === 'verifying' ? (
            <p>Verifying your magic link.</p>
          ) : (
            <p className="text-destructive">{errorMsg}</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
