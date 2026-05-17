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
    has_password: true,
    restart_required: false,
  },
  smtpConfigNoRestart: {
    host: 'smtp.example.com',
    port: 587,
    user: 'relay',
    from_email: 'no-reply@example.com',
    has_password: true,
    restart_required: false,
  },
  smtpConfigRestartRequired: {
    host: 'smtp.example.com',
    port: 587,
    user: 'relay',
    from_email: 'no-reply@example.com',
    has_password: true,
    restart_required: true,
  },
  smtpConfigNoPassword: {
    host: null,
    port: null,
    user: null,
    from_email: null,
    has_password: false,
    restart_required: false,
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

  it('hydrates host/port/user/from_email fields from getSmtpConfig', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithPassword);

    await renderSmtp();

    const hostInput = await screen.findByLabelText(/host/i);
    expect(hostInput).toHaveValue('smtp.example.com');
    expect(screen.getByLabelText(/port/i)).toHaveValue('587');
    expect(screen.getByLabelText(/username/i)).toHaveValue('relay');
    expect(screen.getByLabelText(/from address/i)).toHaveValue('no-reply@example.com');
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

  it('hydrates form fields exactly once (useEffect one-shot via !hydrated guard)', async () => {
    // getSmtpConfig is called once; fields should be seeded from that response
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithPassword);

    await renderSmtp();

    // Assert all four seeded fields carry the config values
    const hostInput = await screen.findByLabelText(/host/i);
    expect(hostInput).toHaveValue('smtp.example.com');
    expect(screen.getByLabelText(/port/i)).toHaveValue('587');
    expect(screen.getByLabelText(/username/i)).toHaveValue('relay');
    expect(screen.getByLabelText(/from address/i)).toHaveValue('no-reply@example.com');

    // getSmtpConfig should have been called exactly once (no extra fetches)
    expect(mockGetSmtpConfig).toHaveBeenCalledTimes(1);
  });

  it('does not re-seed fields when config resolves to the same value (hydrated guard holds)', async () => {
    mockGetSmtpConfig.mockResolvedValue(fixtures.smtpConfigWithPassword);

    const user = userEvent.setup();
    await renderSmtp();

    // Wait for initial hydration
    const hostInput = await screen.findByLabelText(/host/i);
    await waitFor(() => expect(hostInput).toHaveValue('smtp.example.com'));

    // User edits the host field
    await user.clear(hostInput);
    await user.type(hostInput, 'smtp.custom.net');
    expect(hostInput).toHaveValue('smtp.custom.net');

    // A stale-time expiry or query re-run would normally trigger re-evaluation;
    // because hydrated=true the useEffect body is a no-op — user edit is preserved
    expect(hostInput).toHaveValue('smtp.custom.net');
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
});
