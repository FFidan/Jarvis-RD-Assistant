/**
 * AccessModeSection vitest (UI-5)
 *
 * Covers:
 *  1. Renders current mode from mocked getFirstRunStatus.setup_mode.
 *  2. Changing selection and clicking Save calls saveSetupMode with the chosen mode.
 *  3. Shows the persistent restart note at all times.
 *  4. Shows success message after save.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// ---------------------------------------------------------------------------
// Hoisted fixtures
// ---------------------------------------------------------------------------

const fixtures = vi.hoisted(() => ({
  statusSingle: { configured: true, setup_mode: 'single' as const },
  statusMulti: { configured: true, setup_mode: 'multi' as const },
  saveResponse: { mode: 'multi' as const, restart_required: true },
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
const mockGetStatus = vi.mocked(getFirstRunStatus);
const mockSave = vi.mocked(saveSetupMode);

// ---------------------------------------------------------------------------
// Render helper
// ---------------------------------------------------------------------------

function makeQC() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

async function renderSection() {
  const { AccessModeSection } = await import('@/components/settings/AccessModeSection');
  const qc = makeQC();
  return render(
    <QueryClientProvider client={qc}>
      <AccessModeSection />
    </QueryClientProvider>,
  );
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

  it('shows the actionable restart command only once a restart is pending', async () => {
    mockGetStatus.mockResolvedValue(fixtures.statusSingle);
    const user = userEvent.setup();
    await renderSection();

    expect(
      screen.queryByText(/docker compose restart paper_ingestion learning_engine/i),
    ).not.toBeInTheDocument();

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /multi-user/i })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole('radio', { name: /multi-user/i }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(
        screen.getByText(/docker compose restart paper_ingestion learning_engine/i),
      ).toBeInTheDocument(),
    );
  });

  it('shows success message after save', async () => {
    mockGetStatus.mockResolvedValue(fixtures.statusSingle);
    const user = userEvent.setup();
    await renderSection();

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /multi-user/i })).toBeInTheDocument(),
    );

    await user.click(screen.getByRole('radio', { name: /multi-user/i }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(
        screen.getByText(/saved — restart required for the change to take effect/i),
      ).toBeInTheDocument(),
    );
  });

  it('shows the pending pill after a restart-required save and persists it', async () => {
    mockGetStatus.mockResolvedValue(fixtures.statusSingle);
    const user = userEvent.setup();
    await renderSection();

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /multi-user/i })).toBeInTheDocument(),
    );

    await user.click(screen.getByRole('radio', { name: /multi-user/i }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(screen.getByText(/mode change pending — restart required/i)).toBeInTheDocument(),
    );
    expect(localStorage.getItem('jarvis-access-mode-pending')).toBe('multi');
  });

  it('restores the pending pill from localStorage and clears it once the API reports the mode', async () => {
    localStorage.setItem('jarvis-access-mode-pending', 'multi');
    // API still reports the old mode → pill should be visible.
    mockGetStatus.mockResolvedValue(fixtures.statusSingle);
    const { unmount } = await renderSection();

    await waitFor(() =>
      expect(screen.getByText(/mode change pending — restart required/i)).toBeInTheDocument(),
    );
    unmount();

    // After a restart the API now reports the saved mode → pill clears.
    mockGetStatus.mockResolvedValue(fixtures.statusMulti);
    await renderSection();

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /multi-user/i })).toBeChecked(),
    );
    expect(screen.queryByText(/mode change pending/i)).not.toBeInTheDocument();
    await waitFor(() =>
      expect(localStorage.getItem('jarvis-access-mode-pending')).toBeNull(),
    );
  });

  it('shows the no-restart success message when restart_required is false', async () => {
    mockGetStatus.mockResolvedValue(fixtures.statusMulti);
    mockSave.mockResolvedValue({ mode: 'single', restart_required: false });
    const user = userEvent.setup();
    await renderSection();

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /single-user/i })).toBeInTheDocument(),
    );

    await user.click(screen.getByRole('radio', { name: /single-user/i }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(screen.getByText(/access mode updated/i)).toBeInTheDocument(),
    );
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
