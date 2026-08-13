import { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import type { SessionUser } from '@/stores/auth-store';
import { useAuthStore } from '@/stores/auth-store';
import { ApiError, verifyMagicLink } from '@/lib/api';
import { errorMessage } from '@/lib/errors';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

/**
 * Handles the magic-link landing URL.
 *
 * URL shape: /auth/verify#token=<urlsafe-32>.
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

function isRetryableSignInError(err: unknown): boolean {
  if (err instanceof ApiError) {
    return err.status === 0 || [500, 502, 503, 504].includes(err.status);
  }
  return err instanceof TypeError;
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function verifyWithRetry(token: string): Promise<SessionUser> {
  const delays = [250, 750];
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await verifyMagicLink(token);
    } catch (err) {
      if (!isRetryableSignInError(err) || attempt >= delays.length) {
        throw err;
      }
      await wait(delays[attempt] ?? 0);
    }
  }
}

function verifyOnce(token: string): Promise<SessionUser> {
  let p = inflightVerifications.get(token);
  if (!p) {
    p = verifyWithRetry(token).catch((err) => {
      inflightVerifications.delete(token);
      throw err;
    });
    inflightVerifications.set(token, p);
  }
  return p;
}

function clearVerification(token: string): void {
  inflightVerifications.delete(token);
}

/** Exposed only for test tear-down — call in beforeEach to prevent Map leakage across tests. */
export function __resetVerifyDedupeForTests(): void {
  inflightVerifications.clear();
}

export function AuthVerifyPage() {
  const [, setSearchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { loginWithSession, isAuthenticated, isSessionValid } = useAuthStore();
  const [status, setStatus] = useState<'verifying' | 'error'>('verifying');
  const [errorMsg, setErrorMsg] = useState('');
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const tokenRef = useRef(
    new URLSearchParams(location.hash.startsWith('#') ? location.hash.slice(1) : location.hash)
      .get('token'),
  );

  useEffect(() => {
    const tokenInAddress = new URLSearchParams(
      location.hash.startsWith('#') ? location.hash.slice(1) : location.hash,
    ).get('token');
    if (!tokenInAddress) return;
    // Replacing the search params also clears the fragment. Keep the token in
    // memory just long enough to exchange it, never in browser history.
    setSearchParams({}, { replace: true });
    // The initial URL is captured exactly once; subsequent router updates must
    // not replace the in-memory single-use token.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (isAuthenticated && isSessionValid()) {
      navigate('/', { replace: true });
      return;
    }

    const token = tokenRef.current;
    if (!token) {
      navigate('/login?error=Missing+token', { replace: true });
      return;
    }

    let cancelled = false;
    void (async () => {
      try {
        const user = await verifyOnce(token);
        if (cancelled) {
          clearVerification(token);
          return;
        }
        await loginWithSession(user);
        clearVerification(token);
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
  }, [isAuthenticated, isSessionValid, loginWithSession, navigate]);

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
