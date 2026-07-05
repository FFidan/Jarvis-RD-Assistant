/**
 * Account — self-service profile + email for the authenticated user.
 *
 * - Loads profile via GET /api/account (fetchAccount).
 * - display_name edit via PATCH /api/account (updateAccount).
 * - Email change via PATCH /api/account with `email` field; response carries
 *   `email_verification_sent: true` when a verify link was sent.
 * - Confirm-email-change token: when the page mounts with
 *   `?confirm_email_token=<tok>` in the URL, this component calls
 *   confirmEmailChange(tok) and shows success/failure, then strips the param
 *   without adding a new route.
 */
import { useEffect, useRef, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { useSearchParams } from 'react-router-dom';
import { fetchAccount, updateAccount, confirmEmailChange, downloadMyData } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent } from '@/components/ui/card';
import { Pencil, Check, X, User, Mail, ShieldCheck, Download } from 'lucide-react';
import { errorMessage } from '@/lib/errors';
import type { AccountResponse } from '@/types';

// ---------------------------------------------------------------------------
// Inline helpers
// ---------------------------------------------------------------------------

function formatDate(iso: string | null | undefined): string {
  if (!iso) return 'Never';
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

// ---------------------------------------------------------------------------
// Confirm-token banner (shown when ?confirm_email_token= is present on mount)
// ---------------------------------------------------------------------------

type ConfirmState =
  | { status: 'idle' }
  | { status: 'pending' }
  | { status: 'ok'; email: string }
  | { status: 'err'; message: string };

function useConfirmEmailToken(): ConfirmState {
  const [searchParams, setSearchParams] = useSearchParams();
  const token = searchParams.get('confirm_email_token');
  const [state, setState] = useState<ConfirmState>({ status: 'idle' });
  const confirmedRef = useRef(false);
  const qc = useQueryClient();

  useEffect(() => {
    if (!token || confirmedRef.current) return;
    confirmedRef.current = true;
    setState({ status: 'pending' });

    confirmEmailChange(token)
      .then((account: AccountResponse) => {
        setState({ status: 'ok', email: account.email });
        qc.invalidateQueries({ queryKey: QUERY_KEYS.account.self() });
        // Strip the token from the URL without a full navigation
        const next = new URLSearchParams(searchParams);
        next.delete('confirm_email_token');
        setSearchParams(next, { replace: true });
      })
      .catch((err: unknown) => {
        const message = errorMessage(err, 'Email confirmation failed. The link may have expired.');
        setState({ status: 'err', message });
        const next = new URLSearchParams(searchParams);
        next.delete('confirm_email_token');
        setSearchParams(next, { replace: true });
      });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  return state;
}

function ConfirmBanner({ state }: { state: ConfirmState }) {
  if (state.status === 'idle') return null;
  if (state.status === 'pending') {
    return (
      <div className="rounded-md bg-[hsl(var(--muted))] border border-hair px-4 py-3 text-sm text-muted-foreground">
        Confirming email change…
      </div>
    );
  }
  if (state.status === 'ok') {
    return (
      <div className="rounded-md bg-[hsl(var(--status-ok)_/_0.1)] border border-[hsl(var(--status-ok)_/_0.4)] px-4 py-3 text-sm text-[hsl(var(--status-ok))]">
        Email address updated to <strong>{state.email}</strong>.
      </div>
    );
  }
  // err
  return (
    <div className="rounded-md bg-[hsl(var(--destructive)_/_0.08)] border border-[hsl(var(--destructive)_/_0.3)] px-4 py-3 text-sm text-destructive">
      {state.message}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Display-name edit row
// ---------------------------------------------------------------------------

function DisplayNameRow({ account }: { account: AccountResponse }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);

  const mut = useMutation({
    mutationFn: (display_name: string | null) => updateAccount({ display_name }),
    onSuccess: (res) => {
      // Update cache directly from response so UI reflects immediately
      qc.setQueryData(['account'], res.account);
      setEditing(false);
      setError(null);
    },
    onError: (e: unknown) => {
      setError(errorMessage(e, 'Failed to save display name'));
    },
  });

  const startEdit = () => {
    setDraft(account.display_name ?? '');
    setEditing(true);
    setError(null);
  };

  const save = () => {
    const trimmed = draft.trim();
    mut.mutate(trimmed.length > 0 ? trimmed : null);
  };

  const cancel = () => {
    setEditing(false);
    setError(null);
  };

  return (
    <div className="flex flex-col gap-1.5">
      <Label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <User className="h-3.5 w-3.5" />
        Display name
      </Label>
      {editing ? (
        <div className="flex items-center gap-2">
          <Input
            aria-label="Display name"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="h-8 text-sm max-w-xs"
            onKeyDown={(e) => {
              if (e.key === 'Enter') save();
              if (e.key === 'Escape') cancel();
            }}
            autoFocus
          />
          <Button
            size="icon"
            variant="ghost"
            className="h-7 w-7"
            aria-label="Save display name"
            onClick={save}
            disabled={mut.isPending}
          >
            <Check className="h-3.5 w-3.5" />
          </Button>
          <Button
            size="icon"
            variant="ghost"
            className="h-7 w-7"
            aria-label="Cancel display name edit"
            onClick={cancel}
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <span className="text-sm" data-testid="display-name-value">
            {account.display_name ?? <span className="text-muted-foreground italic">Not set</span>}
          </span>
          <Button
            size="icon"
            variant="ghost"
            className="h-7 w-7"
            aria-label="Edit display name"
            onClick={startEdit}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Email-change row
// ---------------------------------------------------------------------------

type EmailChangeState = 'idle' | 'editing' | 'sent';

function EmailRow({ account }: { account: AccountResponse }) {
  const qc = useQueryClient();
  const [state, setState] = useState<EmailChangeState>('idle');
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);

  const mut = useMutation({
    mutationFn: (email: string) => updateAccount({ email }),
    onSuccess: (res) => {
      if (res.email_verification_sent) {
        setState('sent');
        setError(null);
      } else {
        // Unlikely (server should always send a link), but handle gracefully.
        qc.invalidateQueries({ queryKey: QUERY_KEYS.account.self() });
        setState('idle');
      }
    },
    onError: (e: unknown) => {
      setError(errorMessage(e, 'Failed to request email change'));
    },
  });

  const startEdit = () => {
    setDraft(account.email);
    setState('editing');
    setError(null);
  };

  const submit = () => {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === account.email) {
      setState('idle');
      return;
    }
    mut.mutate(trimmed);
  };

  const cancel = () => {
    setState('idle');
    setError(null);
  };

  if (state === 'sent') {
    return (
      <div className="flex flex-col gap-1.5">
        <Label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Mail className="h-3.5 w-3.5" />
          Email
        </Label>
        <div className="rounded-md bg-[hsl(var(--status-ok)_/_0.1)] border border-[hsl(var(--status-ok)_/_0.4)] px-3 py-2 text-sm text-[hsl(var(--status-ok))]">
          Verification link sent to <strong>{draft}</strong>. Click the link in that email to confirm.
        </div>
        <Button
          size="sm"
          variant="ghost"
          className="self-start"
          onClick={() => setState('idle')}
        >
          OK
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1.5">
      <Label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Mail className="h-3.5 w-3.5" />
        Email
      </Label>
      {state === 'editing' ? (
        <div className="flex items-center gap-2">
          <Input
            aria-label="New email address"
            type="email"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="h-8 text-sm max-w-xs"
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit();
              if (e.key === 'Escape') cancel();
            }}
            autoFocus
          />
          <Button
            size="sm"
            variant="default"
            onClick={submit}
            disabled={mut.isPending}
          >
            Send verification
          </Button>
          <Button size="sm" variant="ghost" onClick={cancel}>
            Cancel
          </Button>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <span className="text-sm" data-testid="email-value">{account.email}</span>
          <Button
            size="icon"
            variant="ghost"
            className="h-7 w-7"
            aria-label="Change email"
            onClick={startEdit}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
      <p className="text-xs text-muted-foreground">
        A verification link will be sent to the new address before the change takes effect.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Account data export
// ---------------------------------------------------------------------------

function AccountDataExportCard() {
  const [downloadStarted, setDownloadStarted] = useState(false);

  const mut = useMutation({
    mutationFn: downloadMyData,
    onSuccess: () => {
      setDownloadStarted(true);
    },
  });

  const startDownload = () => {
    setDownloadStarted(false);
    mut.mutate();
  };

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardContent className="p-5 space-y-3">
        <div className="flex items-center gap-2">
          <Download className="h-4 w-4 text-muted-foreground" />
          <span className="text-sm font-semibold">Account data export</span>
        </div>
        <p className="text-sm text-muted-foreground">
          Download a ZIP of your account data, including papers saved to your library and your
          private workspace records.
        </p>
        <Button size="sm" variant="outline" onClick={startDownload} disabled={mut.isPending}>
          {mut.isPending ? 'Preparing download…' : 'Download my data'}
        </Button>
        {downloadStarted && (
          <p className="text-xs text-[hsl(var(--status-ok))]">Download started.</p>
        )}
        {mut.isError && (
          <p className="text-xs text-destructive" role="alert">
            Account data export could not be downloaded. Please try again.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// AccountSection — public export
// ---------------------------------------------------------------------------

export function AccountSection() {
  const confirmState = useConfirmEmailToken();

  const { data: account, isLoading, isError } = useQuery({
    queryKey: QUERY_KEYS.account.self(),
    queryFn: fetchAccount,
  });

  if (isLoading) {
    return <div className="py-8 text-center text-muted-foreground text-sm">Loading profile…</div>;
  }
  if (isError || !account) {
    return (
      <div className="py-8 text-center text-sm text-destructive">
        Failed to load account profile. Please refresh.
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <p className="text-sm text-muted-foreground">
        Manage your display name, email address, and account details.
      </p>

      {/* Confirm-email banner (shown only when ?confirm_email_token= was in URL) */}
      <ConfirmBanner state={confirmState} />

      {/* Profile card */}
      <Card className="rounded-md border-hair shadow-none">
        <CardContent className="p-5 space-y-5">
          <div className="flex items-center gap-2">
            <User className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm font-semibold">Profile</span>
          </div>

          <DisplayNameRow account={account} />
          <EmailRow account={account} />

          <div className="flex flex-col gap-1.5">
            <Label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <ShieldCheck className="h-3.5 w-3.5" />
              Role
            </Label>
            <span className="text-sm capitalize" data-testid="role-value">{account.role}</span>
          </div>
        </CardContent>
      </Card>

      <AccountDataExportCard />

      {/* Metadata card */}
      <Card className="rounded-md border-hair shadow-none">
        <CardContent className="p-5">
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <p className="text-xs text-muted-foreground mb-0.5">Member since</p>
              <p>{formatDate(account.created_at)}</p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground mb-0.5">Last login</p>
              <p>{formatDate(account.last_login_at)}</p>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
