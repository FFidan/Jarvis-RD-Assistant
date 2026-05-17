/**
 * TelegramBotTokenSection vitest (UI-4)
 *
 * Covers:
 *  1. Renders "A bot token is configured" when getTelegramBotToken returns has_token=true.
 *  2. Renders "No bot token set" when getTelegramBotToken returns has_token=false.
 *  3. Entering a valid token and clicking Save calls saveTelegramBotToken with the value.
 *  4. Shows the persistent restart note at all times.
 *  5. Shows a format error when the token doesn't match the expected pattern.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// ---------------------------------------------------------------------------
// Hoisted fixtures
// ---------------------------------------------------------------------------

const fixtures = vi.hoisted(() => ({
  hasToken: { has_token: true },
  noToken: { has_token: false },
  saveOk: { saved: true, restart_required: true },
}));

// ---------------------------------------------------------------------------
// API mock
// ---------------------------------------------------------------------------

vi.mock('@/lib/api', () => ({
  getTelegramBotToken: vi.fn(),
  saveTelegramBotToken: vi.fn(),
}));

// auth-store mock (required by api.ts module)
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: vi.fn(() => ({
      getApiKey: vi.fn(() => 'test-key'),
      logout: vi.fn(),
    })),
  },
}));

import { getTelegramBotToken, saveTelegramBotToken } from '@/lib/api';
const mockGet = vi.mocked(getTelegramBotToken);
const mockSave = vi.mocked(saveTelegramBotToken);

// ---------------------------------------------------------------------------
// Render helper
// ---------------------------------------------------------------------------

function makeQC() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

async function renderSection() {
  const { TelegramBotTokenSection } = await import(
    '@/components/settings/TelegramBotTokenSection'
  );
  const qc = makeQC();
  return render(
    <QueryClientProvider client={qc}>
      <TelegramBotTokenSection />
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('TelegramBotTokenSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSave.mockResolvedValue(fixtures.saveOk);
  });

  it('shows "A bot token is configured" when has_token=true', async () => {
    mockGet.mockResolvedValue(fixtures.hasToken);
    await renderSection();

    await waitFor(() =>
      expect(screen.getByText(/a bot token is configured/i)).toBeInTheDocument(),
    );
  });

  it('shows "No bot token set" when has_token=false', async () => {
    mockGet.mockResolvedValue(fixtures.noToken);
    await renderSection();

    await waitFor(() =>
      expect(screen.getByText(/no bot token set/i)).toBeInTheDocument(),
    );
  });

  it('shows the persistent restart note', async () => {
    mockGet.mockResolvedValue(fixtures.noToken);
    await renderSection();

    await waitFor(() =>
      expect(
        screen.getByText(/telegram bot must be restarted by an administrator/i),
      ).toBeInTheDocument(),
    );
  });

  it('calls saveTelegramBotToken with the entered token on Save', async () => {
    mockGet.mockResolvedValue(fixtures.noToken);
    const user = userEvent.setup();
    await renderSection();

    await waitFor(() =>
      expect(screen.getByLabelText(/bot token/i)).toBeInTheDocument(),
    );

    const input = screen.getByLabelText(/bot token/i);
    // Valid token format: <digits>:<20+ alphanumeric/dash/underscore>
    await user.type(input, '123456789:ABCdefGHIjklMNOpqrSTU');

    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockSave).toHaveBeenCalled();
      expect(mockSave.mock.calls[0]?.[0]).toBe('123456789:ABCdefGHIjklMNOpqrSTU');
    });
  });

  it('shows a format error for an invalid token and does NOT call save', async () => {
    mockGet.mockResolvedValue(fixtures.noToken);
    const user = userEvent.setup();
    await renderSection();

    await waitFor(() =>
      expect(screen.getByLabelText(/bot token/i)).toBeInTheDocument(),
    );

    const input = screen.getByLabelText(/bot token/i);
    await user.type(input, 'bad-token');

    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(screen.getByText(/invalid format/i)).toBeInTheDocument(),
    );
    expect(mockSave).not.toHaveBeenCalled();
  });

  it('Save button is disabled when token input is empty', async () => {
    mockGet.mockResolvedValue(fixtures.noToken);
    await renderSection();

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument(),
    );

    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });
});
