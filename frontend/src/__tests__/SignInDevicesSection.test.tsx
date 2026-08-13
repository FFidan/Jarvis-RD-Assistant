/**
 * SignInDevicesSection — Settings → Account → Passkeys.
 *
 * Covers every UX state: capability-unavailable (browser + LAN), empty list,
 * populated list (added / last-used), register success, register duplicate
 * (409), register cancelled (WebAuthnError), and the last-vs-not-last revoke
 * warnings. The 401 mid-session-revoke path is covered in the auth401 spec.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// --- Passkey library mock (shared with the hook via the same module) ---
const browserSupportsWebAuthnMock = vi.fn().mockReturnValue(true);
const startRegistrationMock = vi.fn();

// Hoisted so the synchronous module-mock factory can reference it eagerly.
const { MockWebAuthnError } = vi.hoisted(() => {
  class MockWebAuthnError extends Error {
    code: string;
    // Default to the code the library emits for a user cancel / ceremony timeout
    // (a passed-through NotAllowedError), not the abort-signal code.
    constructor(code = 'ERROR_PASSTHROUGH_SEE_CAUSE_PROPERTY') {
      super('webauthn ceremony error');
      this.name = 'WebAuthnError';
      this.code = code;
    }
  }
  return { MockWebAuthnError };
});

vi.mock('@simplewebauthn/browser', () => ({
  browserSupportsWebAuthn: () => browserSupportsWebAuthnMock(),
  startRegistration: (opts: unknown) => startRegistrationMock(opts),
  startAuthentication: vi.fn(),
  WebAuthnError: MockWebAuthnError,
}));

// --- API mock (real ApiError preserved via the spread) ---
const getPasskeyCapabilityMock = vi.fn();
const listPasskeysMock = vi.fn();
const beginPasskeyRegistrationMock = vi.fn();
const finishPasskeyRegistrationMock = vi.fn();
const deletePasskeyMock = vi.fn();

vi.mock('@/lib/api', async (importActual) => ({
  ...(await importActual<typeof import('@/lib/api')>()),
  getPasskeyCapability: () => getPasskeyCapabilityMock(),
  listPasskeys: () => listPasskeysMock(),
  beginPasskeyRegistration: () => beginPasskeyRegistrationMock(),
  finishPasskeyRegistration: (c: unknown, n?: string) => finishPasskeyRegistrationMock(c, n),
  deletePasskey: (id: string) => deletePasskeyMock(id),
}));

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: vi.fn(() => ({
      logout: vi.fn(),
      loginWithSession: vi.fn(),
    })),
  },
}));

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

const passkey = (over: Partial<Record<string, unknown>> = {}) => ({
  id: 'cred-1',
  nickname: 'Chrome on macOS',
  transports: ['internal'],
  created_at: '2026-07-01T00:00:00Z',
  last_used_at: '2026-07-05T00:00:00Z',
  ...over,
});

function makeQC() {
  return createTestQueryClient();
}

async function renderSection() {
  const { SignInDevicesSection } = await import('@/components/settings/SignInDevicesSection');
  const qc = makeQC();
  return {
    qc,
    ...renderWithProviders(
      <SignInDevicesSection />,
      { queryClient: qc },
    ),
  };
}

describe('SignInDevicesSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    browserSupportsWebAuthnMock.mockReturnValue(true);
    getPasskeyCapabilityMock.mockResolvedValue({ available: true, access_mode: 'localhost' });
    listPasskeysMock.mockResolvedValue([]);
  });

  // --- capability-unavailable ---

  it('explains when the browser cannot do passkeys (no controls)', async () => {
    browserSupportsWebAuthnMock.mockReturnValue(false);
    await renderSection();
    expect(await screen.findByText(/browser does not support passkeys/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /add a passkey/i })).not.toBeInTheDocument();
  });

  it('shows the LAN note when the server reports passkeys unavailable here', async () => {
    getPasskeyCapabilityMock.mockResolvedValue({ available: false, access_mode: 'lan' });
    await renderSection();
    expect(
      await screen.findByText(/passkeys work on the jarvis computer itself/i),
    ).toBeInTheDocument();
  });

  // --- list states ---

  it('renders the empty state when the user has no passkeys', async () => {
    listPasskeysMock.mockResolvedValue([]);
    await renderSection();
    expect(await screen.findByText(/no passkeys yet/i)).toBeInTheDocument();
  });

  it('lists registered passkeys with added / last-used metadata', async () => {
    listPasskeysMock.mockResolvedValue([passkey()]);
    await renderSection();
    expect(await screen.findByText('Chrome on macOS')).toBeInTheDocument();
    expect(screen.getByText(/added/i)).toHaveTextContent(/last used/i);
  });

  // --- register ceremony ---

  it('registers a passkey and confirms success', async () => {
    const user = userEvent.setup();
    beginPasskeyRegistrationMock.mockResolvedValue({ challenge: 'x' });
    startRegistrationMock.mockResolvedValue({ id: 'att' });
    finishPasskeyRegistrationMock.mockResolvedValue({ id: 'cred-1', nickname: 'Chrome on macOS' });
    const { toast } = await import('sonner');
    await renderSection();

    await user.click(await screen.findByRole('button', { name: /add a passkey/i }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /create passkey/i }));

    await waitFor(() => expect(finishPasskeyRegistrationMock).toHaveBeenCalled());
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Passkey added.'));
  });

  it('shows a duplicate message when the credential is already registered (409)', async () => {
    const { ApiError } = await import('@/lib/api');
    const user = userEvent.setup();
    beginPasskeyRegistrationMock.mockResolvedValue({ challenge: 'x' });
    startRegistrationMock.mockResolvedValue({ id: 'att' });
    finishPasskeyRegistrationMock.mockRejectedValue(new ApiError(409, '{"detail":"exists"}'));
    await renderSection();

    await user.click(await screen.findByRole('button', { name: /add a passkey/i }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /create passkey/i }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/already has a passkey/i);
  });

  it('shows a cancelled message when the user dismisses the authenticator', async () => {
    const user = userEvent.setup();
    beginPasskeyRegistrationMock.mockResolvedValue({ challenge: 'x' });
    startRegistrationMock.mockRejectedValue(
      new MockWebAuthnError('ERROR_PASSTHROUGH_SEE_CAUSE_PROPERTY'),
    );
    await renderSection();

    await user.click(await screen.findByRole('button', { name: /add a passkey/i }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /create passkey/i }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/cancelled or timed out/i);
  });

  it('reports a config fault honestly instead of blaming a cancel', async () => {
    const user = userEvent.setup();
    beginPasskeyRegistrationMock.mockResolvedValue({ challenge: 'x' });
    // A SecurityError → ERROR_INVALID_DOMAIN: a config fault, not a cancel.
    startRegistrationMock.mockRejectedValue(new MockWebAuthnError('ERROR_INVALID_DOMAIN'));
    await renderSection();

    await user.click(await screen.findByRole('button', { name: /add a passkey/i }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /create passkey/i }));

    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent(/complete passkey sign-in here/i);
    expect(alert).not.toHaveTextContent(/cancelled or timed out/i);
  });

  // --- revoke ---

  it('warns that this is the only passkey before revoking the last one', async () => {
    const user = userEvent.setup();
    listPasskeysMock.mockResolvedValue([passkey()]);
    await renderSection();

    await user.click(await screen.findByRole('button', { name: /remove passkey chrome on macos/i }));
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/only passkey/i)).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: /^remove$/i }));
    await waitFor(() => expect(deletePasskeyMock).toHaveBeenCalledWith('cred-1'));
  });

  it('does not show the last-passkey warning when other passkeys remain', async () => {
    const user = userEvent.setup();
    listPasskeysMock.mockResolvedValue([passkey(), passkey({ id: 'cred-2', nickname: 'Firefox on Linux' })]);
    await renderSection();

    await user.click(await screen.findByRole('button', { name: /remove passkey firefox on linux/i }));
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).queryByText(/only passkey/i)).not.toBeInTheDocument();
    expect(within(dialog).getByText(/other passkeys keep working/i)).toBeInTheDocument();
  });
});

describe('defaultPasskeyNickname', () => {
  it('derives a "Browser on OS" label from the user agent', async () => {
    const { defaultPasskeyNickname } = await import('@/components/settings/SignInDevicesSection');
    expect(
      defaultPasskeyNickname(
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120.0 Safari/537.36',
      ),
    ).toBe('Chrome on macOS');
  });
});
