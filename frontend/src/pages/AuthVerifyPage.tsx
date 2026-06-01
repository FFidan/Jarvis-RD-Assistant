import { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useAuthStore } from '@/stores/auth-store';
import { verifyMagicLink } from '@/lib/api';
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
 * StrictMode-safety: React 18 double-invokes effects in dev. We use a ref
 * guard so we only POST once even if the effect re-runs.
 */
export function AuthVerifyPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { loginWithSession } = useAuthStore();
  const [status, setStatus] = useState<'verifying' | 'error'>('verifying');
  const [errorMsg, setErrorMsg] = useState('');
  const ranOnceRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (ranOnceRef.current) return;
    ranOnceRef.current = true;

    const token = searchParams.get('token');
    if (!token) {
      navigate('/login?error=Missing+token', { replace: true });
      return;
    }

    let cancelled = false;
    void (async () => {
      try {
        const user = await verifyMagicLink(token);
        if (cancelled) return;
        loginWithSession(user);
        navigate('/', { replace: true });
      } catch (err) {
        if (cancelled) return;
        const message =
          err instanceof Error && err.message
            ? err.message
            : 'Invalid or expired link';
        setErrorMsg(message);
        setStatus('error');
        // Brief display window then redirect; this gives the user a chance
        // to read what went wrong before getting bounced back to /login.
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
