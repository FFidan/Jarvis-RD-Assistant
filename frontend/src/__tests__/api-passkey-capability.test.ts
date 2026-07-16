import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getPasskeyCapability } from '@/lib/api';

describe('getPasskeyCapability', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  // A same-origin GET omits the Origin header (Fetch standard), so the backend probe
  // would see origin=None and hide every passkey control in production. The probe MUST
  // be a POST so the browser attaches Origin on the same-origin production request.
  it('probes capability with a POST so the browser attaches Origin', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ available: true, access_mode: 'localhost' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await getPasskeyCapability();

    expect(globalThis.fetch).toHaveBeenCalledWith(
      '/api/auth/passkeys/capability',
      expect.objectContaining({ method: 'POST' }),
    );
  });
});
