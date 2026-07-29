/**
 * AccessModeSection vitest (UI-5)
 *
 * Covers:
 *  1. Renders current mode from mocked getFirstRunStatus.setup_mode.
 *  2. Changing selection and clicking Save calls saveSetupMode with the chosen mode.
 *  3. Shows plain confirmation after a successful save (no restart text).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QUERY_KEYS } from '@/lib/query-keys';

// ---------------------------------------------------------------------------
// Hoisted fixtures
// ---------------------------------------------------------------------------

const fixtures = vi.hoisted(() => ({
  statusSingle: { configured: true, setup_mode: 'single' as const },
  statusMulti: { configured: true, setup_mode: 'multi' as const },
  saveResponse: { mode: 'multi' as const, restart_required: false },
}));

// ---------------------------------------------------------------------------
// API mock
// ---------------------------------------------------------------------------

vi.mock('@/lib/api', () => ({
  getFirstRunStatus: vi.fn(),
  saveSetupMode: vi.fn(),
}));

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: vi.fn(() => ({
      getApiKey: vi.fn(() => 'test-key'),
      logout: vi.fn(),
    })),
  },
}));

import { getFirstRunStatus, saveSetupMode } from '@/lib/api';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';
const mockGetStatus = vi.mocked(getFirstRunStatus);
const mockSave = vi.mocked(saveSetupMode);

// ---------------------------------------------------------------------------
// Render helper
// ---------------------------------------------------------------------------

function makeQC() {
  return createTestQueryClient();
}

async function renderSection() {
  const { AccessModeSection } = await import('@/components/settings/AccessModeSection');
  const qc = makeQC();
  return {
    qc,
    ...renderWithProviders(
      <AccessModeSection />,
      { queryClient: qc },
    ),
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AccessModeSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    mockSave.mockResolvedValue(fixtures.saveResponse);
  });

  it('renders "Single-user" radio as selected when setup_mode=single', async () => {
    mockGetStatus.mockResolvedValue(fixtures.statusSingle);
    await renderSection();

    await waitFor(() => {
      const singleRadio = screen.getByRole('radio', { name: /single-user/i });
      expect(singleRadio).toBeChecked();
    });

    const multiRadio = screen.getByRole('radio', { name: /multi-user/i });
    expect(multiRadio).not.toBeChecked();
  });

  it('renders "Multi-user" radio as selected when setup_mode=multi', async () => {
    mockGetStatus.mockResolvedValue(fixtures.statusMulti);
    await renderSection();

    await waitFor(() => {
      const multiRadio = screen.getByRole('radio', { name: /multi-user/i });
      expect(multiRadio).toBeChecked();
    });
  });

  it('calls saveSetupMode with "multi" after selecting multi-user and saving', async () => {
    mockGetStatus.mockResolvedValue(fixtures.statusSingle);
    const user = userEvent.setup();
    await renderSection();

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /single-user/i })).toBeInTheDocument(),
    );

    const multiRadio = screen.getByRole('radio', { name: /multi-user/i });
    await user.click(multiRadio);

    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockSave).toHaveBeenCalled();
      expect(mockSave.mock.calls[0]?.[0]).toBe('multi');
    });
  });

  it('updates the setup-status cache after a successful save', async () => {
    mockGetStatus
      .mockResolvedValueOnce(fixtures.statusSingle)
      .mockResolvedValue(fixtures.statusMulti);
    const user = userEvent.setup();
    const { qc } = await renderSection();

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /multi-user/i })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole('radio', { name: /multi-user/i }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(qc.getQueryData<{ setup_mode?: string }>(QUERY_KEYS.setup.firstRun())?.setup_mode).toBe('multi');
    });
  });

  it('shows the plain confirmation after a successful save', async () => {
    mockGetStatus.mockResolvedValue(fixtures.statusSingle);
    const user = userEvent.setup();
    await renderSection();

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /multi-user/i })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole('radio', { name: /multi-user/i }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(screen.getByText(/sign-in method updated/i)).toBeInTheDocument(),
    );
  });

  it('never shows a restart pill or compose command', async () => {
    mockGetStatus.mockResolvedValue(fixtures.statusSingle);
    const user = userEvent.setup();
    await renderSection();

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /multi-user/i })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole('radio', { name: /multi-user/i }));
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(screen.getByText(/sign-in method updated/i)).toBeInTheDocument(),
    );

    expect(screen.queryByText(/restart/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/docker compose/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(localStorage.getItem('jarvis-access-mode-pending')).toBeNull();
  });

  // Anti-tautology: with restart_required:false the HEAD component already hides
  // the pill (its onSuccess clears pendingRestartMode), so the negative test above
  // passes on broken AND fixed code. This variant forces the response to claim a
  // restart so the ONLY way the pill/compose text can stay absent is to delete the
  // restart JSX entirely. FAILS on HEAD (the pill + compose paragraph render on a
  // restart_required:true save), PASSES after the dead-UI removal.
  it('ignores a restart_required:true response — no pill, no compose command, no localStorage write', async () => {
    mockSave.mockResolvedValue({ mode: 'multi', restart_required: true });
    mockGetStatus.mockResolvedValue(fixtures.statusSingle);
    const user = userEvent.setup();
    await renderSection();

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /multi-user/i })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole('radio', { name: /multi-user/i }));
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(screen.getByText(/sign-in method updated/i)).toBeInTheDocument(),
    );

    expect(screen.queryByText(/restart/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/docker compose/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(localStorage.getItem('jarvis-access-mode-pending')).toBeNull();
  });

  it('does not claim single-user restricts login and accurately describes what the toggle controls', async () => {
    mockGetStatus.mockResolvedValue(fixtures.statusSingle);
    await renderSection();

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /single-user/i })).toBeInTheDocument(),
    );

    // The old false promise must be gone.
    expect(screen.queryByText(/only the admin account can log in/i)).not.toBeInTheDocument();

    // The new label must describe the sign-in screen / login method offered.
    expect(screen.getAllByText(/sign-in screen/i).length).toBeGreaterThan(0);
  });
});
