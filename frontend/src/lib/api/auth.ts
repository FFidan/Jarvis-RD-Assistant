// Auth (magic-link), passkeys (WebAuthn), admin user management, and admin audit log.
import { apiFetchJson, apiFetchVoid } from './core';
import {
  adminUserListSchema,
  adminUserSchema,
  auditLogPageSchema,
  ownerIdentitySchema,
  passkeyCapabilitySchema,
  passkeyCountSchema,
  passkeyCreationOptionsSchema,
  passkeyInfoListSchema,
  passkeyRegistrationResultSchema,
  passkeyRequestOptionsSchema,
  sendSignInLinkSchema,
  sentResponseSchema,
  sessionUserSchema,
} from './schemas/auth';
export type {
  AdminUser,
  AuditLogEntry,
  AuditLogPage,
  OwnerIdentity,
  PasskeyCapability,
  PasskeyInfo,
} from './schemas/auth';
import type {
  AuthenticationResponseJSON,
  RegistrationResponseJSON,
} from '@simplewebauthn/browser';

// --- Auth (magic-link) ---

/** Request a one-shot magic-link email. Always resolves true regardless of
 *  whether the email exists (the backend deliberately doesn't leak account
 *  existence). Throws ApiError only on network/transport failure. */
export const requestMagicLink = (email: string) =>
  apiFetchJson('/api/auth/request-link', sentResponseSchema, {
    method: 'POST',
    body: JSON.stringify({ email }),
  });

/** Exchange a magic-link token for a session cookie + user record. */
export const verifyMagicLink = (token: string) =>
  apiFetchJson('/api/auth/verify', sessionUserSchema, {
    method: 'POST',
    body: JSON.stringify({ token }),
  });

/** Revoke the current session and clear the cookie. */
export const logoutSession = () =>
  apiFetchVoid('/api/auth/logout', { method: 'POST' });

// --- Passkeys (WebAuthn) ---
//
// Every call below flows through the shared decoded client, so a 401 on a session-scoped route
// (e.g. an admin revoked all of your passkeys/sessions mid-session) is handled
// by the shared auto-logout path in ./core — no bespoke 401 handling here.

/** Whether the server will accept passkeys for the current request origin, plus
 *  the active access mode so the UI can explain when it can't (unauthenticated). */
/** Probe whether passkeys are usable from this origin (unauthenticated). POST (not
 *  GET) so the browser attaches the Origin header on the same-origin production
 *  request — a same-origin GET omits it, which would hide every passkey control. */
export const getPasskeyCapability = () =>
  apiFetchJson('/api/auth/passkeys/capability', passkeyCapabilitySchema, { method: 'POST' });

/** Fetch WebAuthn creation options to hand straight to `startRegistration`. */
export const beginPasskeyRegistration = () =>
  apiFetchJson(
    '/api/auth/passkeys/register/begin',
    passkeyCreationOptionsSchema,
    { method: 'POST' },
  );

/** Complete registration; the credential is the attestation from the browser. */
export const finishPasskeyRegistration = (
  credential: RegistrationResponseJSON,
  nickname?: string,
) =>
  apiFetchJson(
    '/api/auth/passkeys/register/finish',
    passkeyRegistrationResultSchema,
    { method: 'POST', body: JSON.stringify({ ...credential, nickname }) },
  );

/** Fetch WebAuthn request options to hand straight to `startAuthentication`. */
export const beginPasskeyLogin = () =>
  apiFetchJson(
    '/api/auth/passkeys/login/begin',
    passkeyRequestOptionsSchema,
    { method: 'POST' },
  );

/** Complete login; sets the session cookie and returns the signed-in user. */
export const finishPasskeyLogin = (assertion: AuthenticationResponseJSON) =>
  apiFetchJson('/api/auth/passkeys/login/finish', sessionUserSchema, {
    method: 'POST',
    body: JSON.stringify(assertion),
  });

/** List the current user's registered passkeys. Requires a session. */
export const listPasskeys = () =>
  apiFetchJson('/api/auth/passkeys', passkeyInfoListSchema);

/** Revoke one of the current user's passkeys. Requires a session. */
export const deletePasskey = (credentialId: string) =>
  apiFetchVoid(`/api/auth/passkeys/${encodeURIComponent(credentialId)}`, { method: 'DELETE' });

/** Admin: how many passkeys a user has (recovery-planning signal). */
export const getUserPasskeyCount = (userId: number) =>
  apiFetchJson(`/api/admin/users/${userId}/passkeys`, passkeyCountSchema);

/** Admin: revoke every passkey a user holds (pairs with a fresh sign-in link). */
export const revokeAllUserPasskeys = (userId: number) =>
  apiFetchVoid(`/api/admin/users/${userId}/passkeys/revoke-all`, {
    method: 'POST',
  });

// --- Admin user management ---

/** List users, including soft-deleted ones still within the 30-day restore
 *  grace (so the admin UI can offer a restore action). Requires admin role. */
export const listUsers = () =>
  apiFetchJson('/api/admin/users?include_deleted=true', adminUserListSchema);

/** Invite a new user. Sends them a 24-hour magic link. Requires admin role. */
export const inviteUser = (email: string, role: 'user' | 'admin') =>
  apiFetchJson('/api/admin/users', adminUserSchema, {
    method: 'POST',
    body: JSON.stringify({ email, role }),
  });

/** Change a user's role. Requires admin role. */
export const updateUserRole = (userId: number, role: 'user' | 'admin') =>
  apiFetchJson(`/api/admin/users/${userId}/role`, adminUserSchema, {
    method: 'PATCH',
    body: JSON.stringify({ role }),
  });

/** Soft-delete a user (sets deleted_at). Requires admin role. */
export const deleteUser = (userId: number) =>
  apiFetchVoid(`/api/admin/users/${userId}`, { method: 'DELETE' });

/** Restore a soft-deleted user within the 30-day grace. Requires admin role. */
export const restoreUser = (userId: number) =>
  apiFetchJson(`/api/admin/users/${userId}/restore`, adminUserSchema, { method: 'POST' });

/** Transfer database-managed ownership to another live administrator. */
export const transferOwner = (targetUserId: number, confirmation: string) =>
  apiFetchJson('/api/admin/owner/transfer', ownerIdentitySchema, {
    method: 'POST',
    body: JSON.stringify({ target_user_id: targetUserId, confirmation }),
  });

export const sendSignInLink = (userId: number) =>
  apiFetchJson(
    `/api/admin/users/${userId}/send-link`,
    sendSignInLinkSchema,
    { method: 'POST' },
  );

// --- Admin audit log ---

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
  return apiFetchJson(`/api/admin/audit-log${suffix}`, auditLogPageSchema);
};
