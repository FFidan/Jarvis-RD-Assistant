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

  it('shows the persistent restart note', async () => {
    mockGetStatus.mockResolvedValue(fixtures.statusSingle);
    await renderSection();

    await waitFor(() =>
      expect(
        screen.getByText(/changing access mode requires an application restart/i),
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
      expect(screen.getByText(/restart required/i)).toBeInTheDocument(),
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
});
