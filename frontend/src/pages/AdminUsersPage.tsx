/**
 * Admin user management page.
 *
 * Accessible at /admin/users. Requires admin role; non-admins are redirected
 * to / by the route guard in App.tsx.
 *
 * Features:
 * - Table of users with email, role, created_at, last_login_at, actions. Soft-
 *   deleted users still inside the 30-day grace are shown with a restore action.
 * - "Invite user" button opens a modal (email + role selector → POST).
 * - Per-row role change (dropdown select).
 * - Per-row soft-delete with confirmation; per-row restore for deleted users.
 */

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { formatDistanceToNow } from 'date-fns';
import { toast } from 'sonner';
import {
  listUsers,
  inviteUser,
  updateUserRole,
  deleteUser,
  restoreUser,
  transferOwner,
  sendSignInLink,
  getUserPasskeyCount,
  revokeAllUserPasskeys,
  type AdminUser,
  type FirstRunStatus,
} from '@/lib/api';
import { useAuthStore } from '@/stores/auth-store';
import { ApiError } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
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
import { Badge } from '@/components/ui/badge';
import {
  ArrowRightLeft,
  UserPlus,
  Trash2,
  Shield,
  User,
  Send,
  RotateCcw,
  KeyRound,
} from 'lucide-react';
import { AdminBreadcrumb } from '@/components/layout/AdminBreadcrumb';

function formatDate(iso: string | null): string {
  if (!iso) return '—';
  try {
    return formatDistanceToNow(new Date(iso), { addSuffix: true });
  } catch {
    return iso;
  }
}

// ---------------------------------------------------------------------------
// Invite modal
// ---------------------------------------------------------------------------

interface InviteModalProps {
  open: boolean;
  onClose: () => void;
}

function InviteModal({ open, onClose }: InviteModalProps) {
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<'user' | 'admin'>('user');
  const [error, setError] = useState<string | null>(null);
  const [manualLink, setManualLink] = useState<string | null>(null);
  const queryClient = useQueryClient();

  // Cache-only read of the SMTP-readiness signal warmed by the pre-auth
  // /api/setup/status query (mirrors LoginPage). No extra network request.
  const smtpConfigured = queryClient.getQueryData<FirstRunStatus>(
    QUERY_KEYS.setup.firstRun(),
  )?.smtp_configured;

  const { mutate, isPending } = useMutation({
    mutationFn: () => inviteUser(email.trim(), role),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.admin.users() });
      setError(null);
      // The backend returns invite_link only when the email could not be
      // delivered. Surface it so the admin can share it manually; keep the
      // modal open in that case so the link stays visible.
      const link = data.invite_link;
      if (link) {
        setManualLink(link);
      } else {
        setEmail('');
        setRole('user');
        onClose();
      }
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        // The user already exists — point the admin at the working per-row
        // rescue action instead of leaving a generic dead-end.
        setError(
          "A user with that email already exists. To send them a fresh sign-in link, " +
            "use the 'Send sign-in link' action on their row below.",
        );
      } else if (err instanceof ApiError) {
        setError(err.detail);
      } else {
        setError('Failed to invite user. Please try again.');
      }
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim()) return;
    setError(null);
    mutate();
  }

  function handleClose() {
    setEmail('');
    setRole('user');
    setError(null);
    setManualLink(null);
    onClose();
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && handleClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Invite user</DialogTitle>
        </DialogHeader>
        {manualLink ? (
          <div className="space-y-4">
            <p className="text-sm text-muted-foreground" role="status">
              {smtpConfigured === false
                ? 'Automatic email is not configured.'
                : 'JARVIS could not deliver the invite email.'}{' '}
              Share this sign-in link with the user manually — it expires in 24 hours.
            </p>
            <div className="space-y-2">
              <Label htmlFor="invite-link">Sign-in link</Label>
              <Input
                id="invite-link"
                type="text"
                readOnly
                value={manualLink}
                onFocus={(e) => e.currentTarget.select()}
                aria-label="Invite sign-in link"
              />
            </div>
            <DialogFooter>
              <Button type="button" onClick={handleClose}>
                Done
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            {smtpConfigured === false && (
              <p className="text-sm text-muted-foreground" role="status">
                SMTP is not configured. After inviting, you&apos;ll get a link to
                share with the user manually.
              </p>
            )}
            <div className="space-y-2">
              <Label htmlFor="invite-email">Email address</Label>
              <Input
                id="invite-email"
                type="email"
                placeholder="user@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                autoFocus
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="invite-role">Role</Label>
              <Select value={role} onValueChange={(v) => setRole(v as 'user' | 'admin')}>
                <SelectTrigger id="invite-role">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="user">User</SelectItem>
                  <SelectItem value="admin">Admin</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {error && (
              <p className="text-sm text-destructive" role="alert">
                {error}
              </p>
            )}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={handleClose} disabled={isPending}>
                Cancel
              </Button>
              <Button type="submit" disabled={isPending || !email.trim()}>
                {isPending ? 'Sending invite…' : 'Send invite'}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Delete confirmation
// ---------------------------------------------------------------------------

interface DeleteConfirmProps {
  user: AdminUser | null;
  onConfirm: () => void;
  onCancel: () => void;
}

function DeleteConfirm({ user, onConfirm, onCancel }: DeleteConfirmProps) {
  return (
    <AlertDialog open={user !== null} onOpenChange={(open) => !open && onCancel()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Remove user</AlertDialogTitle>
          <AlertDialogDescription>
            Are you sure you want to remove <strong>{user?.email}</strong>? This will revoke their
            access. The action can be undone by re-inviting the same email.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel onClick={onCancel}>Cancel</AlertDialogCancel>
          <AlertDialogAction onClick={onConfirm} className="bg-destructive text-destructive-foreground hover:bg-destructive/90">
            Remove
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

// ---------------------------------------------------------------------------
// Passkey count + revoke-all (per row)
// ---------------------------------------------------------------------------

function PasskeyCell({ user }: { user: AdminUser }) {
  const queryClient = useQueryClient();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const isDeleted = Boolean(user.deleted_at);

  const { data, isLoading } = useQuery({
    queryKey: QUERY_KEYS.passkeys.adminCount(user.id),
    queryFn: () => getUserPasskeyCount(user.id),
    enabled: !isDeleted,
  });
  const count = data?.count ?? 0;

  const revokeMutation = useMutation({
    mutationFn: () => revokeAllUserPasskeys(user.id),
    onSuccess: () => {
      queryClient.setQueryData(QUERY_KEYS.passkeys.adminCount(user.id), { count: 0 });
      void queryClient.invalidateQueries({
        queryKey: QUERY_KEYS.passkeys.adminCount(user.id),
      });
      setConfirmOpen(false);
      toast.success(`Revoked all passkeys for ${user.email}`);
    },
    onError: (err) => {
      setConfirmOpen(false);
      toast.error(err instanceof ApiError ? err.detail : 'Failed to revoke passkeys.');
    },
  });

  if (isDeleted) {
    return <span className="text-muted-foreground">—</span>;
  }

  return (
    <div className="flex items-center gap-1.5">
      <span className="text-muted-foreground tabular-nums">
        {isLoading ? '…' : count}
      </span>
      {count > 0 && (
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 text-muted-foreground hover:text-destructive"
          disabled={revokeMutation.isPending}
          onClick={() => setConfirmOpen(true)}
          aria-label={`Revoke all passkeys for ${user.email}`}
          title={`Revoke all passkeys for ${user.email}`}
        >
          <KeyRound className="h-4 w-4" />
        </Button>
      )}
      <AlertDialog open={confirmOpen} onOpenChange={(open) => !open && setConfirmOpen(false)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Revoke all passkeys</AlertDialogTitle>
            <AlertDialogDescription>
              Remove every passkey for <strong>{user.email}</strong>? They&apos;ll need to
              sign in with a fresh link and re-register a passkey. Use the
              &ldquo;Send sign-in link&rdquo; action afterwards to get them back in.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setConfirmOpen(false)}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => revokeMutation.mutate()}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Revoke all
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function AdminUsersPage() {
  const currentUser = useAuthStore((s) => s.user);
  const queryClient = useQueryClient();
  const smtpConfigured = queryClient.getQueryData<FirstRunStatus>(
    QUERY_KEYS.setup.firstRun(),
  )?.smtp_configured;

  const [inviteOpen, setInviteOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<AdminUser | null>(null);
  const [roleError, setRoleError] = useState<string | null>(null);
  const [pendingRoleUserId, setPendingRoleUserId] = useState<number | null>(null);
  // Track which specific user's delete is in-flight so only
  // that row's button is disabled, not all rows (same pattern as pendingRoleUserId).
  const [pendingDeleteUserId, setPendingDeleteUserId] = useState<number | null>(null);
  // Per-row send-link in-flight tracking (same isolation pattern as delete):
  // only the targeted row's button is disabled, not the whole table.
  const [pendingSendLinkUserId, setPendingSendLinkUserId] = useState<number | null>(null);
  const [pendingRestoreUserId, setPendingRestoreUserId] = useState<number | null>(null);
  const [pendingTransfer, setPendingTransfer] = useState<AdminUser | null>(null);
  const [transferConfirmation, setTransferConfirmation] = useState('');
  const [transferError, setTransferError] = useState<string | null>(null);
  const [manualSignInLink, setManualSignInLink] = useState<{ email: string; link: string } | null>(
    null,
  );

  const { data: users, isLoading, isError } = useQuery({
    queryKey: QUERY_KEYS.admin.users(),
    queryFn: listUsers,
  });

  const roleMutation = useMutation({
    mutationFn: ({ userId, role }: { userId: number; role: 'user' | 'admin' }) =>
      updateUserRole(userId, role),
    onMutate: ({ userId }) => setPendingRoleUserId(userId),
    onSettled: () => setPendingRoleUserId(null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.admin.users() });
      setRoleError(null);
    },
    onError: (err) => {
      if (err instanceof ApiError) {
        setRoleError(err.detail);
      } else {
        setRoleError('Failed to update role.');
      }
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (userId: number) => deleteUser(userId),
    onMutate: (userId) => setPendingDeleteUserId(userId),
    onSettled: () => setPendingDeleteUserId(null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.admin.users() });
      setPendingDelete(null);
    },
    onError: (err) => {
      setPendingDelete(null);
      toast.error(err instanceof ApiError ? err.detail : 'Failed to remove user.');
    },
  });

  const restoreMutation = useMutation({
    mutationFn: (userId: number) => restoreUser(userId),
    onMutate: (userId) => setPendingRestoreUserId(userId),
    onSettled: () => setPendingRestoreUserId(null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.admin.users() });
    },
    onError: (err) => {
      toast.error(err instanceof ApiError ? err.detail : 'Failed to restore user.');
    },
  });

  const sendLinkMutation = useMutation({
    mutationFn: ({ userId }: { userId: number; email: string }) => sendSignInLink(userId),
    onMutate: ({ userId }) => setPendingSendLinkUserId(userId),
    onSettled: () => setPendingSendLinkUserId(null),
    onSuccess: (data, { email }) => {
      if (data.sent_link) {
        setManualSignInLink({ email, link: data.sent_link });
      } else {
        toast.success(`Sign-in link sent to ${email}`);
      }
    },
    onError: (err) => {
      toast.error(
        err instanceof ApiError ? err.detail : 'Failed to send sign-in link.',
      );
    },
  });

  const transferMutation = useMutation({
    mutationFn: ({ user, confirmation }: { user: AdminUser; confirmation: string }) =>
      transferOwner(user.id, confirmation),
    onSuccess: (_data, { user }) => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.admin.users() });
      toast.success(`Ownership transferred to ${user.email}`);
      setPendingTransfer(null);
      setTransferConfirmation('');
      setTransferError(null);
    },
    onError: (err) => {
      setTransferError(
        err instanceof ApiError ? err.detail : 'Failed to transfer ownership.',
      );
    },
  });

  const ownerRecord = users?.find((user) => user.owner_state != null);
  const currentOwner = users?.find((user) => user.is_owner);
  const ownerSource = ownerRecord?.owner_source;
  const ownerState = ownerRecord?.owner_state;
  const canTransferOwnership =
    ownerSource === 'database' &&
    ownerState === 'valid' &&
    currentOwner?.id === currentUser?.id;

  function closeTransferDialog() {
    if (transferMutation.isPending) return;
    setPendingTransfer(null);
    setTransferConfirmation('');
    setTransferError(null);
  }

  if (isLoading) {
    return (
      <div className="p-6 text-sm text-muted-foreground">Loading users…</div>
    );
  }

  if (isError) {
    return (
      <div className="p-6 text-sm text-destructive">Failed to load users.</div>
    );
  }

  return (
    <div className="p-4 sm:p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <AdminBreadcrumb page="Users" />
          <h1 className="text-2xl font-semibold">User management</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Invite and manage users who can access this JARVIS instance.
          </p>
        </div>
        <Button onClick={() => setInviteOpen(true)}>
          <UserPlus className="h-4 w-4 mr-2" />
          Invite user
        </Button>
      </div>

      {roleError && (
        <p className="text-sm text-destructive" role="alert">
          {roleError}
        </p>
      )}

      {ownerSource === 'environment' && (
        <div className="rounded-md border bg-muted/40 px-4 py-3 text-sm" role="status">
          Ownership is managed on the host with OWNER_USER_ID. Change that value and restart
          JARVIS to choose a different owner.
        </div>
      )}

      {ownerState != null && ownerState !== 'valid' && ownerSource !== 'environment' && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/5 px-4 py-3 text-sm" role="status">
          JARVIS does not have a valid instance owner. On the server, run{' '}
          <code>jarvis-research owner status</code>, then{' '}
          <code>jarvis-research owner set &lt;admin-email&gt;</code>.
        </div>
      )}

      {ownerState === 'valid' && (
        <p className="text-sm text-muted-foreground">
          The operations key can recover only the instance owner account. It is not a shared
          family password.
        </p>
      )}

      <div className="rounded-md border overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/50">
              <th className="px-4 py-3 text-left font-medium">Email</th>
              <th className="px-4 py-3 text-left font-medium">Role</th>
              <th className="px-4 py-3 text-left font-medium">Joined</th>
              <th className="px-4 py-3 text-left font-medium">Last login</th>
              <th className="px-4 py-3 text-left font-medium">Passkeys</th>
              <th className="px-4 py-3 text-left font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users?.map((user) => {
              const isSelf = currentUser?.id === user.id;
              const isDeleted = Boolean(user.deleted_at);
              return (
                <tr key={user.id} className="border-b last:border-0">
                  <td className="px-4 py-3">
                    <span className="font-medium">{user.email}</span>
                    {isSelf && (
                      <Badge variant="outline" className="ml-2 text-xs">
                        you
                      </Badge>
                    )}
                    {user.is_owner && (
                      <Badge variant="outline" className="ml-2 text-xs">
                        instance owner
                      </Badge>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <Select
                      value={user.role}
                      onValueChange={(v) =>
                        roleMutation.mutate({ userId: user.id, role: v as 'user' | 'admin' })
                      }
                      disabled={isSelf || user.is_owner || pendingRoleUserId === user.id}
                    >
                      <SelectTrigger className="w-28 h-8 text-xs" aria-label={`Role for ${user.email}`}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="user">
                          <span className="flex items-center gap-1.5">
                            <User className="h-3 w-3" />
                            User
                          </span>
                        </SelectItem>
                        <SelectItem value="admin">
                          <span className="flex items-center gap-1.5">
                            <Shield className="h-3 w-3" />
                            Admin
                          </span>
                        </SelectItem>
                      </SelectContent>
                    </Select>
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {formatDate(user.created_at)}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {formatDate(user.last_login_at)}
                  </td>
                  <td className="px-4 py-3">
                    <PasskeyCell user={user} />
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1">
                      {isDeleted ? (
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8 text-muted-foreground hover:text-foreground"
                          disabled={pendingRestoreUserId === user.id}
                          onClick={() => restoreMutation.mutate(user.id)}
                          aria-label={`Restore ${user.email}`}
                          title={`Restore ${user.email}`}
                        >
                          <RotateCcw className="h-4 w-4" />
                        </Button>
                      ) : (
                        <>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-8 w-8 text-muted-foreground hover:text-foreground"
                            disabled={pendingSendLinkUserId === user.id}
                            onClick={() =>
                              sendLinkMutation.mutate({ userId: user.id, email: user.email })
                            }
                            aria-label={`Send sign-in link to ${user.email}`}
                            title={`Send sign-in link to ${user.email}`}
                          >
                            <Send className="h-4 w-4" />
                          </Button>
                          {canTransferOwnership && user.role === 'admin' && !isSelf && (
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8 text-muted-foreground hover:text-foreground"
                              onClick={() => {
                                setPendingTransfer(user);
                                setTransferConfirmation('');
                                setTransferError(null);
                              }}
                              aria-label={`Transfer ownership to ${user.email}`}
                              title={`Transfer ownership to ${user.email}`}
                            >
                              <ArrowRightLeft className="h-4 w-4" />
                            </Button>
                          )}
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-8 w-8 text-muted-foreground hover:text-destructive"
                            disabled={isSelf || user.is_owner || pendingDeleteUserId === user.id}
                            onClick={() => setPendingDelete(user)}
                            aria-label={`Remove ${user.email}`}
                            title={
                              user.is_owner
                                ? 'Transfer ownership before removing this account'
                                : isSelf
                                  ? 'Cannot remove your own account'
                                  : `Remove ${user.email}`
                            }
                          >
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
            {users?.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-muted-foreground">
                  No users yet. Invite someone to get started.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <InviteModal open={inviteOpen} onClose={() => setInviteOpen(false)} />

      <DeleteConfirm
        user={pendingDelete}
        onConfirm={() => pendingDelete && deleteMutation.mutate(pendingDelete.id)}
        onCancel={() => setPendingDelete(null)}
      />

      <Dialog
        open={pendingTransfer !== null}
        onOpenChange={(open) => !open && closeTransferDialog()}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Transfer ownership</DialogTitle>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              if (!pendingTransfer || transferConfirmation !== pendingTransfer.email) return;
              transferMutation.mutate({
                user: pendingTransfer,
                confirmation: transferConfirmation,
              });
            }}
          >
            <p className="text-sm text-muted-foreground">
              This moves operations-key recovery to {pendingTransfer?.email}. Your own passkeys
              and sign-in links continue to work.
            </p>
            <div className="space-y-2">
              <Label htmlFor="owner-transfer-confirmation">
                Type {pendingTransfer?.email} to confirm
              </Label>
              <Input
                id="owner-transfer-confirmation"
                value={transferConfirmation}
                onChange={(event) => setTransferConfirmation(event.target.value)}
                autoComplete="off"
                autoFocus
              />
            </div>
            {transferError && (
              <p className="text-sm text-destructive" role="alert">
                {transferError}
              </p>
            )}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={closeTransferDialog}
                disabled={transferMutation.isPending}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={
                  transferMutation.isPending ||
                  transferConfirmation !== pendingTransfer?.email
                }
              >
                {transferMutation.isPending ? 'Transferring…' : 'Transfer ownership'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={manualSignInLink !== null}
        onOpenChange={(v) => !v && setManualSignInLink(null)}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Sign-in link</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <p className="text-sm text-muted-foreground" role="status">
              {smtpConfigured === false
                ? 'Automatic email is not configured.'
                : 'JARVIS could not deliver the sign-in email.'}{' '}
              Share this link with {manualSignInLink?.email} manually — it expires in 15 minutes.
            </p>
            <div className="space-y-2">
              <Label htmlFor="signin-link">Sign-in link</Label>
              <Input
                id="signin-link"
                type="text"
                readOnly
                value={manualSignInLink?.link ?? ''}
                onFocus={(e) => e.currentTarget.select()}
                aria-label="Sign-in link to share"
              />
            </div>
            <DialogFooter>
              <Button type="button" onClick={() => setManualSignInLink(null)}>
                Done
              </Button>
            </DialogFooter>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
