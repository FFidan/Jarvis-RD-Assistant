/**
 * Admin user management page.
 *
 * Accessible at /admin/users. Requires admin role; non-admins are redirected
 * to / by the route guard in App.tsx.
 *
 * Features:
 * - Table of all non-deleted users with email, role, created_at, last_login_at, actions.
 * - "Invite user" button opens a modal (email + role selector → POST).
 * - Per-row role change (dropdown select).
 * - Per-row soft-delete with confirmation.
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
  sendSignInLink,
  type AdminUser,
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
import { UserPlus, Trash2, Shield, User, Send } from 'lucide-react';
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
  const queryClient = useQueryClient();

  const { mutate, isPending } = useMutation({
    mutationFn: () => inviteUser(email.trim(), role),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.admin.users() });
      setEmail('');
      setRole('user');
      setError(null);
      onClose();
    },
    onError: (err) => {
      if (err instanceof ApiError) {
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
    onClose();
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && handleClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Invite user</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
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
    <AlertDialog open={user !== null}>
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
// Main page
// ---------------------------------------------------------------------------

export function AdminUsersPage() {
  const currentUser = useAuthStore((s) => s.user);
  const queryClient = useQueryClient();

  const [inviteOpen, setInviteOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<AdminUser | null>(null);
  const [roleError, setRoleError] = useState<string | null>(null);
  const [pendingRoleUserId, setPendingRoleUserId] = useState<number | null>(null);
  // DOM-F-07 (delete): track which specific user's delete is in-flight so only
  // that row's button is disabled, not all rows (same pattern as pendingRoleUserId).
  const [pendingDeleteUserId, setPendingDeleteUserId] = useState<number | null>(null);
  // Per-row send-link in-flight tracking (same isolation pattern as delete):
  // only the targeted row's button is disabled, not the whole table.
  const [pendingSendLinkUserId, setPendingSendLinkUserId] = useState<number | null>(null);

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
    onError: () => {
      setPendingDelete(null);
    },
  });

  const sendLinkMutation = useMutation({
    mutationFn: ({ userId }: { userId: number; email: string }) => sendSignInLink(userId),
    onMutate: ({ userId }) => setPendingSendLinkUserId(userId),
    onSettled: () => setPendingSendLinkUserId(null),
    onSuccess: (_data, { email }) => {
      toast.success(`Sign-in link sent to ${email}`);
    },
    onError: (err) => {
      toast.error(
        err instanceof ApiError ? err.detail : 'Failed to send sign-in link.',
      );
    },
  });

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

      <div className="rounded-md border overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/50">
              <th className="px-4 py-3 text-left font-medium">Email</th>
              <th className="px-4 py-3 text-left font-medium">Role</th>
              <th className="px-4 py-3 text-left font-medium">Joined</th>
              <th className="px-4 py-3 text-left font-medium">Last login</th>
              <th className="px-4 py-3 text-left font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users?.map((user) => {
              const isSelf = currentUser?.id === user.id;
              // listUsers only returns non-deleted users, but guard defensively
              // so a soft-deleted row (if ever surfaced) hides the send-link
              // action. deleted_at is not in the AdminUser contract.
              const isDeleted = Boolean(
                (user as AdminUser & { deleted_at?: string | null }).deleted_at,
              );
              return (
                <tr key={user.id} className="border-b last:border-0">
                  <td className="px-4 py-3">
                    <span className="font-medium">{user.email}</span>
                    {isSelf && (
                      <Badge variant="outline" className="ml-2 text-xs">
                        you
                      </Badge>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <Select
                      value={user.role}
                      onValueChange={(v) =>
                        roleMutation.mutate({ userId: user.id, role: v as 'user' | 'admin' })
                      }
                      disabled={pendingRoleUserId === user.id}
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
                    <div className="flex items-center gap-1">
                      {!isDeleted && (
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
                      )}
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 text-muted-foreground hover:text-destructive"
                        disabled={isSelf || pendingDeleteUserId === user.id}
                        onClick={() => setPendingDelete(user)}
                        aria-label={`Remove ${user.email}`}
                        title={isSelf ? 'Cannot remove your own account' : `Remove ${user.email}`}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </td>
                </tr>
              );
            })}
            {users?.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-8 text-center text-muted-foreground">
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
    </div>
  );
}
