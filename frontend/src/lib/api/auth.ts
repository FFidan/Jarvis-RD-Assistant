// Auth (magic-link), admin user management, and admin audit log.
import { apiFetch } from './core';
import type { SessionUser } from '@/stores/auth-store';

// --- Auth (magic-link) ---

/** Request a one-shot magic-link email. Always resolves true regardless of
 *  whether the email exists (the backend deliberately doesn't leak account
 *  existence). Throws ApiError only on network/transport failure. */
export const requestMagicLink = (email: string) =>
  apiFetch<{ sent: boolean }>('/api/auth/request-link', {
    method: 'POST',
    body: JSON.stringify({ email }),
  });

/** Exchange a magic-link token for a session cookie + user record. */
export const verifyMagicLink = (token: string) =>
  apiFetch<SessionUser>('/api/auth/verify', {
    method: 'POST',
    body: JSON.stringify({ token }),
  });

/** Revoke the current session and clear the cookie. */
export const logoutSession = () =>
  apiFetch<void>('/api/auth/logout', { method: 'POST' });

// --- Admin user management ---

export interface AdminUser {
  id: number;
  email: string;
  role: 'user' | 'admin';
  created_at: string;
  last_login_at: string | null;
  deleted_at?: string | null;
  invite_link?: string | null;
}

/** List users, including soft-deleted ones still within the 30-day restore
 *  grace (so the admin UI can offer a restore action). Requires admin role. */
export const listUsers = () =>
  apiFetch<AdminUser[]>('/api/admin/users?include_deleted=true');

/** Invite a new user. Sends them a 24-hour magic link. Requires admin role. */
export const inviteUser = (email: string, role: 'user' | 'admin') =>
  apiFetch<AdminUser>('/api/admin/users', {
    method: 'POST',
    body: JSON.stringify({ email, role }),
  });

/** Change a user's role. Requires admin role. */
export const updateUserRole = (userId: number, role: 'user' | 'admin') =>
  apiFetch<AdminUser>(`/api/admin/users/${userId}/role`, {
    method: 'PATCH',
    body: JSON.stringify({ role }),
  });

/** Soft-delete a user (sets deleted_at). Requires admin role. */
export const deleteUser = (userId: number) =>
  apiFetch<void>(`/api/admin/users/${userId}`, { method: 'DELETE' });

/** Restore a soft-deleted user within the 30-day grace. Requires admin role. */
export const restoreUser = (userId: number) =>
  apiFetch<AdminUser>(`/api/admin/users/${userId}/restore`, { method: 'POST' });

export const sendSignInLink = (userId: number) =>
  apiFetch<{ sent: boolean; sent_link?: string | null }>(
    `/api/admin/users/${userId}/send-link`,
    { method: 'POST' },
  );

// --- Admin audit log ---

export interface AuditLogEntry {
  id: number;
  user_id: string | null;
  action: string;
  resource: string;
  metadata: Record<string, unknown> | null;
  created_at: string;
}

export interface AuditLogPage {
  entries: AuditLogEntry[];
  next_before_id: number | null;
}

/** Read the audit log (cursor-paginated, newest first). Requires admin role. */
export const listAuditLog = (params?: {
  limit?: number;
  beforeId?: number | null;
  actionPrefix?: string;
}) => {
  const qs = new URLSearchParams();
  if (params?.limit != null) qs.set('limit', String(params.limit));
  if (params?.beforeId != null) qs.set('before_id', String(params.beforeId));
  if (params?.actionPrefix) qs.set('action_prefix', params.actionPrefix);
  const suffix = qs.toString() ? `?${qs.toString()}` : '';
  return apiFetch<AuditLogPage>(`/api/admin/audit-log${suffix}`);
};
