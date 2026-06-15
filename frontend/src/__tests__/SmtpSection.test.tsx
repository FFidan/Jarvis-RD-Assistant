/**
 * F3 — SmtpSection vitest
 *
 * Covers:
 *  1. SettingsRail shows ONE "Sources" item (not per-source dynamic items).
 *  2. SmtpSection renders and hydrates from a mocked getSmtpConfig.
 *  3. Save calls saveSmtpConfig with the correct shape:
 *       - password field omitted (empty string sent) when left blank + has_password=true
 *       - password field sent when user types a new value
 *
 * vi.mock factories use vi.hoisted() for inline fixtures.
 * Per-file QueryClient (no shared state between tests).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// ---------------------------------------------------------------------------
// Hoisted fixtures — accessible inside vi.mock() factory
// ---------------------------------------------------------------------------

const fixtures = vi.hoisted(() => ({
  smtpConfigWithPassword: {
    host: 'smtp.example.com',
    port: 587,
    user: 'relay',
    from_email: 'no-reply@example.com',
    reply_to: null,
    from_name: null,
    has_password: true,
    restart_required: false,
    deliverable: true,
    issues: [],
  },
  smtpConfigNoRestart: {
    host: 'smtp.example.com',
    port: 587,
    user: 'relay',
    from_email: 'no-reply@example.com',
    reply_to: null,
    from_name: null,
    has_password: true,
    restart_required: false,
    deliverable: true,
    issues: [],
  },
  smtpConfigRestartRequired: {
    host: 'smtp.example.com',
    port: 587,
    user: 'relay',
    from_email: 'no-reply@example.com',
    reply_to: null,
    from_name: null,
    has_password: true,
    restart_required: true,
    deliverable: true,
    issues: [],
  },
  smtpConfigNoPassword: {
    host: null,
    port: null,
    user: null,
    from_email: null,
    reply_to: null,
    from_name: null,
    has_password: false,
    restart_required: false,
    deliverable: false,
    issues: [
      'No mail relay is configured — sign-in links are written to the server log, not emailed.',
    ],
  },
  // host set but From missing → not deliverable (partial config)
  smtpConfigMisconfigured: {
    host: 'smtp.example.com',
    port: 587,
    user: 'relay',
    from_email: null,
    reply_to: null,
    from_name: null,
    has_password: true,
    restart_required: false,
    deliverable: false,
    issues: ['The mail server is set but the From address is missing.'],
  },
  // deliverable config carrying reply-to + display name
  smtpConfigWithReplyTo: {
    host: 'smtp.example.com',
    port: 587,
    user: 'relay',
    from_email: 'no-reply@example.com',
    reply_to: 'replies@example.com',
    from_name: 'JARVIS RD',
    has_password: true,
    restart_required: false,
    deliverable: true,
    issues: [],
  },
  saveResponse: { saved: true, test_sent: null, test_error: null },
}));

// ---------------------------------------------------------------------------
// API mock
// ---------------------------------------------------------------------------

vi.mock('@/lib/api', () => ({
  getSmtpConfig: vi.fn(),
  saveSmtpConfig: vi.fn(),
  // SettingsRail no longer calls fetchSources (collapsed to one item)
  fetchSources: vi.fn().mockResolvedValue([]),
}));

import { getSmtpConfig, saveSmtpConfig } from '@/lib/api';
const mockGetSmtpConfig = vi.mocked(getSmtpConfig);
const mockSaveSmtpConfig = vi.mocked(saveSmtpConfig);

// ---------------------------------------------------------------------------
// Render helpers
// ---------------------------------------------------------------------------

function makeQC() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

async function renderSmtp() {
  // Dynamic import after mocks are set up
  const { SmtpSection } = await import('@/components/settings/SmtpSection');
  const qc = makeQC();
  return render(
    <QueryClientProvider client={qc}>
      <SmtpSection />
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// SettingsRail — Sources collapse test
// ---------------------------------------------------------------------------

describe('SettingsRail — Sources section', () => {
  it('shows exactly ONE "Sources" rail button (not per-source items)', async () => {
    const { SettingsRail } = await import('@/components/settings/SettingsRail');
    const qc = makeQC();
    render(
      <QueryClientProvider client={qc}>
        <SettingsRail
          activeSection="sources"
          activeItem="sources"
          isAdmin={true}
          onSelect={vi.fn()}
        />
      </QueryClientProvider>,
    );

    // Wait for rail to render
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sources' })).toBeInTheDocument());

    // There must be exactly one button labelled "Sources"
    const sourcesButtons = screen.getAllByRole('button', { name: 'Sources' });
    expect(sourcesButtons).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// SmtpSection — hydration
// ---------------------------------------------------------------------------

describe('SmtpSection — hydration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSaveSmtpConfig.mockResolvedValue(fixtures.saveResponse);
  });

  it('hydrates host/port/user/from_email fields from getSmtpConfig (called once)', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithPassword);

    await renderSmtp();

    const hostInput = await screen.findByLabelText(/host/i);
    expect(hostInput).toHaveValue('smtp.example.com');
    expect(screen.getByLabelText(/port/i)).toHaveValue('587');
    expect(screen.getByLabelText(/username/i)).toHaveValue('relay');
    expect(screen.getByLabelText(/from address/i)).toHaveValue('no-reply@example.com');
    // React-Query must not issue duplicate fetches
    expect(mockGetSmtpConfig).toHaveBeenCalledTimes(1);
  });

  it('shows "currently set" hint for password when has_password=true', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithPassword);

    await renderSmtp();

    await waitFor(() => expect(screen.getByText(/currently set/i)).toBeInTheDocument());
  });

  it('does NOT show "currently set" hint when has_password=false', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigNoPassword);

    await renderSmtp();

    // Wait for load to finish
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument(),
    );
    expect(screen.queryByText(/currently set/i)).not.toBeInTheDocument();
  });

  it('does not re-seed fields when config resolves to the same value (hydrated guard holds)', async () => {
    // Return a fresh object reference on each call (simulates React-Query
    // returning a new object after stale-time expiry / background refetch)
    mockGetSmtpConfig.mockImplementation(() =>
      Promise.resolve({ ...fixtures.smtpConfigWithPassword }),
    );

    const user = userEvent.setup();
    const { SmtpSection } = await import('@/components/settings/SmtpSection');
    const qc = makeQC();
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <SmtpSection />
      </QueryClientProvider>,
    );

    // Wait for initial hydration
    const hostInput = await screen.findByLabelText(/host/i);
    await waitFor(() => expect(hostInput).toHaveValue('smtp.example.com'));

    // User edits the host field
    await user.clear(hostInput);
    await user.type(hostInput, 'smtp.custom.net');
    expect(hostInput).toHaveValue('smtp.custom.net');

    // Force a re-render (new QueryClientProvider instance triggers a fresh
    // useEffect call with config — the !hydrated guard must block re-seeding)
    rerender(
      <QueryClientProvider client={qc}>
        <SmtpSection />
      </QueryClientProvider>,
    );

    // User's edit must survive: the !hydrated guard made the useEffect a no-op
    expect(screen.getByLabelText(/host/i)).toHaveValue('smtp.custom.net');
  });
});

// ---------------------------------------------------------------------------
// SmtpSection — Save behaviour
// ---------------------------------------------------------------------------

describe('SmtpSection — Save', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSaveSmtpConfig.mockResolvedValue(fixtures.saveResponse);
  });

  it('calls saveSmtpConfig with empty password string when left blank (has_password=true)', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithPassword);

    const user = userEvent.setup();
    await renderSmtp();

    // Wait for hydration
    const hostInput = await screen.findByLabelText(/host/i);
    await waitFor(() => expect(hostInput).toHaveValue('smtp.example.com'));

    // Do NOT type a password — leave the field blank
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(mockSaveSmtpConfig).toHaveBeenCalled());
    const [firstArg] = mockSaveSmtpConfig.mock.calls[0]!;
    expect(firstArg).toMatchObject({
      host: 'smtp.example.com',
      port: 587,
      user: 'relay',
      from_email: 'no-reply@example.com',
      // password sent as empty string → backend `if body.password:` skips overwrite
      password: '',
    });
  });

  it('calls saveSmtpConfig with the typed password when user fills in a new one', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithPassword);

    const user = userEvent.setup();
    await renderSmtp();

    // Wait for hydration
    const hostInput = await screen.findByLabelText(/host/i);
    await waitFor(() => expect(hostInput).toHaveValue('smtp.example.com'));

    // Type a new password
    const pwField = screen.getByLabelText(/^password/i);
    await user.type(pwField, 'newSecret123');

    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(mockSaveSmtpConfig).toHaveBeenCalled());
    const [firstArg] = mockSaveSmtpConfig.mock.calls[0]!;
    expect(firstArg).toMatchObject({ password: 'newSecret123' });
  });

  it('shows restart-required message after successful save when restart_required=true', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigRestartRequired);
    mockSaveSmtpConfig.mockResolvedValue({ saved: true, test_sent: null, test_error: null });

    const user = userEvent.setup();
    await renderSmtp();

    const hostInput = await screen.findByLabelText(/host/i);
    await waitFor(() => expect(hostInput).toHaveValue('smtp.example.com'));

    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(
        screen.getByText(/administrator must restart the app/i),
      ).toBeInTheDocument(),
    );
  });

  it('does NOT show restart message when restart_required=false (active immediately)', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigNoRestart);
    mockSaveSmtpConfig.mockResolvedValue({ saved: true, test_sent: null, test_error: null });

    const user = userEvent.setup();
    await renderSmtp();

    const hostInput = await screen.findByLabelText(/host/i);
    await waitFor(() => expect(hostInput).toHaveValue('smtp.example.com'));

    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(screen.getByText(/active immediately/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/administrator must restart/i)).not.toBeInTheDocument();
  });

  it('Save button is disabled when host is empty', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigNoPassword);

    await renderSmtp();

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument(),
    );

    // Host field is empty — button should be disabled
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('shows port error and disables Save when port is out of range', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithPassword);

    const user = userEvent.setup();
    await renderSmtp();

    // Wait for hydration
    await screen.findByLabelText(/host/i);

    const portInput = screen.getByLabelText(/port/i);
    await user.clear(portInput);
    await user.type(portInput, '99999');

    expect(screen.getByText(/port must be a number between 1 and 65535/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('shows port error and disables Save when port is non-numeric', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithPassword);

    const user = userEvent.setup();
    await renderSmtp();

    await screen.findByLabelText(/host/i);

    const portInput = screen.getByLabelText(/port/i);
    await user.clear(portInput);
    await user.type(portInput, 'abc');

    expect(screen.getByText(/port must be a number between 1 and 65535/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('enables Save and hides port error when port is valid', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithPassword);

    const user = userEvent.setup();
    await renderSmtp();

    await screen.findByLabelText(/host/i);

    const portInput = screen.getByLabelText(/port/i);
    await user.clear(portInput);
    await user.type(portInput, '99999');

    // Confirm error is shown
    expect(screen.getByText(/port must be a number between 1 and 65535/i)).toBeInTheDocument();

    // Fix the port
    await user.clear(portInput);
    await user.type(portInput, '465');

    expect(screen.queryByText(/port must be a number between 1 and 65535/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).not.toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// SmtpSection — config fetch error
// ---------------------------------------------------------------------------

describe('SmtpSection — config fetch error', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows an error message (not an empty form) when getSmtpConfig fails', async () => {
    mockGetSmtpConfig.mockRejectedValue(new Error('Network error'));

    await renderSmtp();

    await waitFor(() =>
      expect(screen.getByText(/failed to load smtp settings/i)).toBeInTheDocument(),
    );

    // The form must not render — no Save button visible
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// SmtpSection — misconfig banner + sender identity (reply-to / display name)
// ---------------------------------------------------------------------------

describe('SmtpSection — misconfig banner & sender identity', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSaveSmtpConfig.mockResolvedValue(fixtures.saveResponse);
  });

  it('renders the misconfig banner ALONGSIDE the editable form when not deliverable', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigMisconfigured);

    await renderSmtp();

    // Banner is shown...
    expect(await screen.findByTestId('smtp-misconfig-banner')).toHaveTextContent(
      /from address is missing/i,
    );
    // ...and the form stays editable (this is a degraded state, NOT an empty state)
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
    expect(screen.getByLabelText(/host/i)).toBeInTheDocument();
  });

  it('does NOT render the misconfig banner when the relay is deliverable', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithPassword);

    await renderSmtp();
    await screen.findByLabelText(/host/i);

    expect(screen.queryByTestId('smtp-misconfig-banner')).not.toBeInTheDocument();
  });

  it('hydrates reply-to and sender display name from config', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithReplyTo);

    await renderSmtp();

    expect(await screen.findByLabelText(/reply-to/i)).toHaveValue('replies@example.com');
    expect(screen.getByLabelText(/sender display name/i)).toHaveValue('JARVIS RD');
  });

  it('disables Save and shows an error when reply-to is not a valid email', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithPassword);

    const user = userEvent.setup();
    await renderSmtp();
    await screen.findByLabelText(/host/i);

    await user.type(screen.getByLabelText(/reply-to/i), 'not-an-email');

    expect(screen.getByText(/valid email address or leave blank/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('sends reply_to and from_name on save (snake_case, no alias)', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithReplyTo);

    const user = userEvent.setup();
    await renderSmtp();
    await waitFor(() =>
      expect(screen.getByLabelText(/host/i)).toHaveValue('smtp.example.com'),
    );

    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(mockSaveSmtpConfig).toHaveBeenCalled());
    const [firstArg] = mockSaveSmtpConfig.mock.calls[0]!;
    expect(firstArg).toMatchObject({
      reply_to: 'replies@example.com',
      from_name: 'JARVIS RD',
    });
  });

  it('Save & send test email submits test_send=true and reports success', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithPassword);
    mockSaveSmtpConfig.mockResolvedValue({ saved: true, test_sent: true, test_error: null });

    const user = userEvent.setup();
    await renderSmtp();
    await waitFor(() =>
      expect(screen.getByLabelText(/host/i)).toHaveValue('smtp.example.com'),
    );

    await user.click(screen.getByRole('button', { name: /send test email/i }));

    await waitFor(() => expect(mockSaveSmtpConfig).toHaveBeenCalled());
    expect(mockSaveSmtpConfig.mock.calls[0]![0]).toMatchObject({ test_send: true });
    expect(await screen.findByText(/test email sent successfully/i)).toBeInTheDocument();
  });
});
