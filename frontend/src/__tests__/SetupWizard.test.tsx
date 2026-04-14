import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { SetupWizard } from '@/pages/SetupWizard';

vi.mock('@/lib/api', () => ({
  getSetupStatus: vi.fn().mockResolvedValue({
    setup_completed: false,
    models_ready: true,
    models_downloading: [],
    topics_count: 0,
    telegram_configured: false,
    telegram_paired: false,
  }),
  createTopic: vi.fn().mockResolvedValue({ id: 1, name: 'test' }),
  setConfig: vi.fn().mockResolvedValue({ key: 'pulse.cron', value: '0 4 * * *' }),
  markSetupCompleted: vi.fn().mockResolvedValue(undefined),
  createPairingCode: vi.fn(),
  getPairingStatus: vi.fn().mockResolvedValue({ paired: false, chat_id: null }),
  unpairTelegram: vi.fn(),
}));

const api = await import('@/lib/api');

function renderWizard(initialUrl = '/setup?step=1') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialUrl]}>
        <Routes>
          <Route path="/setup" element={<SetupWizard />} />
          <Route path="/" element={<div>HOME</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('SetupWizard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders step 1 welcome screen', () => {
    renderWizard('/setup?step=1');
    expect(screen.getByText('Welcome to JARVIS')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /get started/i })).toBeInTheDocument();
  });

  it('advances to step 2 when Next is clicked', async () => {
    const user = userEvent.setup();
    renderWizard('/setup?step=1');
    await user.click(screen.getByRole('button', { name: /get started/i }));
    expect(await screen.findByText('System check')).toBeInTheDocument();
  });

  it('Skip setup marks completion and navigates home', async () => {
    const user = userEvent.setup();
    renderWizard('/setup?step=1');
    await user.click(screen.getByRole('button', { name: /skip setup/i }));
    await waitFor(() => {
      expect(api.markSetupCompleted).toHaveBeenCalled();
    });
    expect(await screen.findByText('HOME')).toBeInTheDocument();
  });

  it('step 6 calls markSetupCompleted on mount and navigates home on success', async () => {
    renderWizard('/setup?step=6');
    await waitFor(() => {
      expect(api.markSetupCompleted).toHaveBeenCalled();
    });
    // On success, navigation to '/' happens — HOME page should appear.
    expect(await screen.findByText('HOME')).toBeInTheDocument();
  });

  it('step 6 calls markSetupCompleted exactly once (hasTriggered guard)', async () => {
    renderWizard('/setup?step=6');
    await waitFor(() => {
      expect(api.markSetupCompleted).toHaveBeenCalledTimes(1);
    });
  });

  it('step 6 shows error state with retry button when markSetupCompleted fails', async () => {
    vi.mocked(api.markSetupCompleted).mockRejectedValueOnce(new Error('Server error'));
    const user = userEvent.setup();
    renderWizard('/setup?step=6');
    expect(await screen.findByText(/setup completion failed/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
    // Clicking retry fires another mutation call.
    vi.mocked(api.markSetupCompleted).mockResolvedValueOnce(undefined);
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(api.markSetupCompleted).toHaveBeenCalledTimes(2);
    });
  });

  it('renders step 3 topic form', () => {
    renderWizard('/setup?step=3');
    expect(screen.getByText('Your first research topic')).toBeInTheDocument();
    expect(screen.getByLabelText('Topic name')).toBeInTheDocument();
  });

  it('renders step 4 automation form', () => {
    renderWizard('/setup?step=4');
    expect(screen.getByText('Automation schedule')).toBeInTheDocument();
    expect(screen.getByLabelText('Daily run time')).toBeInTheDocument();
  });

  it('renders step 5 telegram pairing', () => {
    renderWizard('/setup?step=5');
    expect(screen.getByText(/pair telegram/i)).toBeInTheDocument();
  });
});
