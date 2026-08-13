import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ApiPayloadError,
  beginPasskeyRegistration,
  listAuditLog,
  verifyMagicLink,
} from '@/lib/api';

describe('authentication API runtime decoding', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('accepts WebAuthn creation options and additive fields', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      rp: { name: 'JARVIS', id: 'localhost' },
      user: { id: 'AQ', name: 'researcher@example.test', displayName: 'Researcher' },
      challenge: 'challenge',
      pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
      authenticatorSelection: {
        residentKey: 'required',
        userVerification: 'required',
      },
      future_webauthn_hint: 'additive',
    })));

    const result = await beginPasskeyRegistration();

    expect(result.user.name).toBe('researcher@example.test');
    expect(result).toMatchObject({ future_webauthn_hint: 'additive' });
  });

  it('rejects malformed nested WebAuthn options before the browser ceremony', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      rp: { name: 'JARVIS' },
      user: { id: 42, name: 'researcher@example.test', displayName: 'Researcher' },
      challenge: 'challenge',
      pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
    })));

    await expect(beginPasskeyRegistration()).rejects.toMatchObject({
      endpoint: '/api/auth/passkeys/register/begin',
      fields: ['user.id'],
    });
  });

  it('rejects an invalid session role without leaking the response body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      id: 1,
      email: 'researcher@example.test',
      role: 'superuser',
      internal_note: 'must-not-appear',
    })));

    const result = verifyMagicLink('valid-length-test-token');

    await expect(result).rejects.toBeInstanceOf(ApiPayloadError);
    await expect(result).rejects.toMatchObject({
      endpoint: '/api/auth/verify',
      fields: ['role'],
    });
    await expect(result).rejects.not.toThrow(/must-not-appear/);
  });

  it('accepts bounded JSON metadata in the administrative audit log', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      entries: [{
        id: 4,
        user_id: '1',
        action: 'auth.passkey.login',
        resource: 'webauthn_credentials',
        metadata: { device: { transports: ['internal'] }, success: true },
        created_at: '2026-08-09T12:00:00Z',
      }],
      next_before_id: null,
    })));

    const result = await listAuditLog();

    expect(result.entries[0]?.metadata).toEqual({
      device: { transports: ['internal'] },
      success: true,
    });
  });
});
