/**
 * usePasskeys — the single React seam over the committed WebAuthn backend.
 *
 * Capability rule: a passkey is only offered when BOTH the browser can do
 * WebAuthn (`browserSupportsWebAuthn()`) AND the server says this request origin
 * is allow-listed (`POST /api/auth/passkeys/capability` → `available`). The server
 * is the source of truth — the browser flag alone never implies capability.
 *
 * All ceremony errors are mapped to a small typed set so callers render one
 * honest message per failure mode instead of leaking raw WebAuthn/HTTP errors.
 * Session-scoped calls (list/delete/register) inherit the shared 401 auto-logout
 * in `lib/api/core`, so a mid-session revoke lands the user back on /login.
 */
import { useCallback, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  browserSupportsWebAuthn,
  startAuthentication,
  startRegistration,
  WebAuthnError,
} from '@simplewebauthn/browser';
import { QUERY_KEYS } from '@/lib/query-keys';
import {
  ApiError,
  beginPasskeyLogin,
  beginPasskeyRegistration,
  deletePasskey,
  finishPasskeyLogin,
  finishPasskeyRegistration,
  getPasskeyCapability,
  listPasskeys,
} from '@/lib/api';
import { useAuthStore } from '@/stores/auth-store';

/** Failure modes a caller renders differently. */
export type PasskeyErrorKind = 'cancelled' | 'duplicate' | 'expired' | 'unsupported' | 'failed';

export interface PasskeyError {
  kind: PasskeyErrorKind;
  message: string;
}

const MESSAGES: Record<PasskeyErrorKind, string> = {
  cancelled: 'Sign-in was cancelled or timed out. You can try again.',
  duplicate: 'This device already has a passkey for your account.',
  expired: 'That request expired before it finished. Please try again.',
  unsupported:
    "This device can't complete passkey sign-in here. Try a different device or ask your admin.",
  failed: "We couldn't complete the passkey step. Please try again.",
};

/** Map a raw ceremony/HTTP error onto a typed, user-facing state. */
export function classifyPasskeyError(err: unknown): PasskeyError {
  if (err instanceof WebAuthnError) {
    // A credential for this account already lives on this device (register only).
    if (err.code === 'ERROR_AUTHENTICATOR_PREVIOUSLY_REGISTERED') {
      return { kind: 'duplicate', message: MESSAGES.duplicate };
    }
    // User cancel or the platform's ceremony timeout: browsers surface both as a
    // NotAllowedError, which @simplewebauthn passes through verbatim under
    // ERROR_PASSTHROUGH_SEE_CAUSE_PROPERTY (see err.cause for the raw
    // NotAllowedError). This is the only retry-able "just try again" ceremony error.
    if (err.code === 'ERROR_PASSTHROUGH_SEE_CAUSE_PROPERTY') {
      return { kind: 'cancelled', message: MESSAGES.cancelled };
    }
    // Every other WebAuthnError (ERROR_INVALID_RP_ID, ERROR_INVALID_DOMAIN,
    // ERROR_CEREMONY_ABORTED, …) is a configuration/environment fault a retry
    // won't fix — say so honestly instead of mislabeling it a cancel.
    return { kind: 'unsupported', message: MESSAGES.unsupported };
  }
  if (err instanceof ApiError) {
    if (err.status === 409) return { kind: 'duplicate', message: MESSAGES.duplicate };
    // A stale/rejected challenge surfaces as a 400 from the finish route; the
    // fix is to start over, so surface it as its own re-request-able state.
    if (err.status === 400) return { kind: 'expired', message: MESSAGES.expired };
    return { kind: 'failed', message: err.detail || MESSAGES.failed };
  }
  return { kind: 'failed', message: MESSAGES.failed };
}

interface UsePasskeysOptions {
  /** Load the current user's passkey list (Settings). Off on the login page. */
  includeList?: boolean;
}

export function usePasskeys(options: UsePasskeysOptions = {}) {
  const { includeList = false } = options;
  const queryClient = useQueryClient();

  // Cheap synchronous browser probe; also gates the network capability query so
  // an unsupported browser (incl. jsdom in tests) never fires a request.
  const browserSupported = browserSupportsWebAuthn();

  const capabilityQuery = useQuery({
    queryKey: QUERY_KEYS.passkeys.capability(),
    queryFn: getPasskeyCapability,
    enabled: browserSupported,
    staleTime: 5 * 60_000,
    retry: false,
  });

  const capable = browserSupported && capabilityQuery.data?.available === true;
  const accessMode = capabilityQuery.data?.access_mode;

  // --- Login ceremony (unauthenticated; used on the login page) ---
  const [loginError, setLoginError] = useState<PasskeyError | null>(null);
  const loginMutation = useMutation({
    mutationFn: async () => {
      const optionsJSON = await beginPasskeyLogin();
      const assertion = await startAuthentication({ optionsJSON });
      return finishPasskeyLogin(assertion);
    },
    onMutate: () => setLoginError(null),
    onSuccess: (user) => {
      // Reuse the shared session-set path (cache purge + SW clear + set state).
      void useAuthStore.getState().loginWithSession(user);
    },
    onError: (err) => setLoginError(classifyPasskeyError(err)),
  });

  // --- Registration ceremony (session; used in Settings) ---
  const [registerError, setRegisterError] = useState<PasskeyError | null>(null);
  const registerMutation = useMutation({
    mutationFn: async (nickname?: string) => {
      const optionsJSON = await beginPasskeyRegistration();
      const attestation = await startRegistration({ optionsJSON });
      return finishPasskeyRegistration(attestation, nickname);
    },
    onMutate: () => setRegisterError(null),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.passkeys.list() });
    },
    onError: (err) => setRegisterError(classifyPasskeyError(err)),
  });

  // --- Device list + revoke (session; used in Settings) ---
  const passkeysQuery = useQuery({
    queryKey: QUERY_KEYS.passkeys.list(),
    queryFn: listPasskeys,
    enabled: includeList && capable,
  });

  const deleteMutation = useMutation({
    mutationFn: deletePasskey,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.passkeys.list() });
    },
  });

  const login = useCallback(() => loginMutation.mutate(), [loginMutation]);
  const resetLoginError = useCallback(() => setLoginError(null), []);
  const resetRegisterError = useCallback(() => setRegisterError(null), []);

  return {
    // capability
    capable,
    browserSupported,
    accessMode,
    capabilityLoading: capabilityQuery.isLoading,
    // login
    login,
    loginPending: loginMutation.isPending,
    loginError,
    resetLoginError,
    // registration
    registerPasskey: (nickname?: string) => registerMutation.mutateAsync(nickname),
    registerPending: registerMutation.isPending,
    registerError,
    resetRegisterError,
    // device list
    passkeys: passkeysQuery.data,
    passkeysLoading: passkeysQuery.isLoading,
    passkeysError: passkeysQuery.isError,
    // revoke
    deletePasskey: (credentialId: string) => deleteMutation.mutateAsync(credentialId),
    deletePending: deleteMutation.isPending,
    deletingId: deleteMutation.variables ?? null,
  };
}
