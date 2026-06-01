import { useState, type FormEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useAuthStore } from '@/stores/auth-store';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { requestMagicLink } from '@/lib/api';

/**
 * Magic-link login surface.
 *
 * Primary: magic-link. User enters email, backend sends a one-shot link.
 * Fallback: API key. Hidden behind a "Use API key instead" toggle so existing
 * users / devs without SMTP can still log in with their JARVIS_API_KEY.
 */
export function LoginPage() {
  const { login } = useAuthStore();
  const [searchParams] = useSearchParams();
  const initialError = searchParams.get('error');

  const [mode, setMode] = useState<'magic-link' | 'api-key'>('magic-link');
  const [email, setEmail] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [error, setError] = useState(initialError ?? '');
  const [info, setInfo] = useState('');
  const [loading, setLoading] = useState(false);

  async function handleMagicLinkSubmit(e: FormEvent) {
    e.preventDefault();
    if (!email.trim()) {
      setError('Email is required');
      return;
    }
    setLoading(true);
    setError('');
    setInfo('');
    try {
      await requestMagicLink(email.trim());
      setInfo('Check your email — we just sent you a sign-in link.');
      setEmail('');
    } catch {
      // Backend returns sent:true unconditionally; a thrown error means a
      // real network/CORS failure, not "email not found". Surface it.
      setError('Could not send link — check your connection and try again.');
    } finally {
      setLoading(false);
    }
  }

  async function handleApiKeySubmit(e: FormEvent) {
    e.preventDefault();
    if (!apiKey.trim()) {
      setError('API key is required');
      return;
    }
    setLoading(true);
    setError('');
    const success = await login(apiKey.trim());
    setLoading(false);
    if (!success) {
      // Surface the precise backend reason (e.g. the 403 multi-tenant-disabled
      // message) rather than a generic string so the user knows to use
      // magic-link instead of retrying a doomed API-key login.
      const backendError = useAuthStore.getState().lastError;
      setError(backendError ?? 'Invalid API key or backend unreachable');
      setApiKey('');
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <CardTitle className="text-2xl">JARVIS RD Assistant</CardTitle>
          <CardDescription>
            {mode === 'magic-link'
              ? 'Enter your email to receive a sign-in link'
              : 'Enter your API key to continue'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {mode === 'magic-link' ? (
            <form onSubmit={handleMagicLinkSubmit} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="email">Email</Label>
                <Input
                  id="email"
                  type="email"
                  value={email}
                  onChange={(e) => {
                    setEmail(e.target.value);
                    setError('');
                    setInfo('');
                  }}
                  placeholder="you@example.com"
                  autoComplete="email"
                  autoFocus
                  disabled={loading}
                />
              </div>
              {error && <p className="text-sm text-destructive">{error}</p>}
              {info && <p className="text-sm text-emerald-600">{info}</p>}
              <Button type="submit" className="w-full" disabled={loading}>
                {loading ? 'Sending...' : 'Send magic link'}
              </Button>
              <button
                type="button"
                className="block w-full text-center text-xs text-muted-foreground hover:underline"
                onClick={() => {
                  setMode('api-key');
                  setError('');
                  setInfo('');
                }}
              >
                Use API key instead
              </button>
            </form>
          ) : (
            <form onSubmit={handleApiKeySubmit} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="apiKey">API Key</Label>
                <Input
                  id="apiKey"
                  type="password"
                  value={apiKey}
                  onChange={(e) => {
                    setApiKey(e.target.value);
                    setError('');
                  }}
                  placeholder="Enter JARVIS_API_KEY"
                  autoComplete="current-password"
                  autoFocus
                  disabled={loading}
                />
              </div>
              {error && <p className="text-sm text-destructive">{error}</p>}
              <Button type="submit" className="w-full" disabled={loading}>
                {loading ? 'Verifying...' : 'Sign In'}
              </Button>
              <button
                type="button"
                className="block w-full text-center text-xs text-muted-foreground hover:underline"
                onClick={() => {
                  setMode('magic-link');
                  setError('');
                }}
              >
                Use magic link instead
              </button>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
