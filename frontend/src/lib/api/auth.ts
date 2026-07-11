// Auth (magic-link), passkeys (WebAuthn), admin user management, and admin audit log.
import { apiFetch } from './core';
import type { SessionUser } from '@/stores/auth-store';
import type {
  AuthenticationResponseJSON,
  PublicKeyCredentialCreationOptionsJSON,
  PublicKeyCredentialRequestOptionsJSON,
  RegistrationResponseJSON,
} from '@simplewebauthn/browser';

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

// --- Passkeys (WebAuthn) ---
//
// Every call below flows through apiFetch, so a 401 on a session-scoped route
// (e.g. an admin revoked all of your passkeys/sessions mid-session) is handled
// by the shared auto-logout path in ./core — no bespoke 401 handling here.

/** Whether the server will accept passkeys for the current request origin, plus
 *  the active access mode so the UI can explain when it can't (unauthenticated). */
export interface PasskeyCapability {
  available: boolean;
  access_mode: string;
}

/** A registered passkey as listed for its owner. */
export interface PasskeyInfo {
  id: string;
  nickname: string;
  transports: string[] | null;
  created_at: string;
  last_used_at: string | null;
}

/** Probe whether passkeys are usable from this origin (unauthenticated). */
export const getPasskeyCapability = () =>
  apiFetch<PasskeyCapability>('/api/auth/passkeys/capability');

/** Fetch WebAuthn creation options to hand straight to `startRegistration`. */
export const beginPasskeyRegistration = () =>
  apiFetch<PublicKeyCredentialCreationOptionsJSON>(
    '/api/auth/passkeys/register/begin',
    { method: 'POST' },
  );

/** Complete registration; the credential is the attestation from the browser. */
export const finishPasskeyRegistration = (
  credential: RegistrationResponseJSON,
  nickname?: string,
) =>
  apiFetch<Pick<PasskeyInfo, 'id' | 'nickname'>>(
    '/api/auth/passkeys/register/finish',
    { method: 'POST', body: JSON.stringify({ ...credential, nickname }) },
  );

/** Fetch WebAuthn request options to hand straight to `startAuthentication`. */
export const beginPasskeyLogin = () =>
  apiFetch<PublicKeyCredentialRequestOptionsJSON>(
    '/api/auth/passkeys/login/begin',
    { method: 'POST' },
  );

/** Complete login; sets the session cookie and returns the signed-in user. */
export const finishPasskeyLogin = (assertion: AuthenticationResponseJSON) =>
  apiFetch<SessionUser>('/api/auth/passkeys/login/finish', {
    method: 'POST',
    body: JSON.stringify(assertion),
  });

/** List the current user's registered passkeys. Requires a session. */
export const listPasskeys = () =>
  apiFetch<PasskeyInfo[]>('/api/auth/passkeys');

/** Revoke one of the current user's passkeys. Requires a session. */
export const deletePasskey = (credentialId: string) =>
  apiFetch<void>(`/api/auth/passkeys/${credentialId}`, { method: 'DELETE' });

/** Admin: how many passkeys a user has (recovery-planning signal). */
export const getUserPasskeyCount = (userId: number) =>
  apiFetch<{ count: number }>(`/api/admin/users/${userId}/passkeys`);

/** Admin: revoke every passkey a user holds (pairs with a fresh sign-in link). */
export const revokeAllUserPasskeys = (userId: number) =>
  apiFetch<void>(`/api/admin/users/${userId}/passkeys/revoke-all`, {
    method: 'POST',
  });

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
