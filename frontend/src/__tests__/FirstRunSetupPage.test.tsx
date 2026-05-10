/**
 * Phase 2 WS-2F: pre-auth first-run wizard tests.
 *
 * Mocks the /api/setup/* surface and asserts:
 *   * step 1 (system check) renders per-service status from the probe response.
 *   * step 3 (admin email) calls createFirstRunAdmin and pushes the user into
 *     the auth store.
 *   * the wizard advances through steps and lands on /done.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

vi.mock('@/lib/api', () => ({
  getFirstRunStatus: vi.fn().mockResolvedValue({ configured: false }),
  runFirstRunSystemCheck: vi.fn().mockResolvedValue({
    services: [
      { name: 'postgres', ok: true, detail: null },
      { name: 'qdrant', ok: true, detail: null },
      { name: 'ollama', ok: false, detail: 'connection refused' },
      { name: 'litellm', ok: true, detail: null },
    ],
    all_ok: false,
  }),
  saveFirstRunSmtp: vi.fn().mockResolvedValue({ saved: true, test_sent: null, test_error: null }),
  createFirstRunAdmin: vi.fn().mockResolvedValue({ id: 1, email: 'admin@example.com', role: 'admin' }),
  saveFirstRunCloudKeys: vi.fn().mockResolvedValue({ saved_providers: [] }),
}));

const api = await import('@/lib/api');
const { FirstRunSetupPage } = await import('@/pages/FirstRunSetupPage');
const { useAuthStore } = await import('@/stores/auth-store');

function renderWizard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/first-run']}>
        <Routes>
          <Route path="/first-run" element={<FirstRunSetupPage />} />
          <Route path="/" element={<div>HOME</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('FirstRunSetupPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({
      isAuthenticated: false,
      authTime: null,
      apiKey: null,
      user: null,
    });
  });

  it('step 1 renders Welcome and runs the system check probe', async () => {
    renderWizard();
    expect(await screen.findByText('Welcome to JARVIS')).toBeInTheDocument();
    await waitFor(() => {
      expect(api.runFirstRunSystemCheck).toHaveBeenCalled();
    });
    // Per-service status must render — postgres OK, ollama unavailable.
    expect(await screen.findByTestId('svc-postgres')).toHaveTextContent(/Ready/i);
    expect(await screen.findByTestId('svc-ollama')).toHaveTextContent(/connection refused/i);
  });

  it('Continue advances from step 1 to step 2 (SMTP)', async () => {
    const user = userEvent.setup();
    renderWizard();
    await screen.findByText('Welcome to JARVIS');
    await user.click(screen.getByRole('button', { name: /continue/i }));
    expect(await screen.findByText('SMTP relay')).toBeInTheDocument();
  });

  it('Skip on SMTP step jumps straight to admin email', async () => {
    const user = userEvent.setup();
    renderWizard();
    await screen.findByText('Welcome to JARVIS');
    await user.click(screen.getByRole('button', { name: /continue/i }));
    await screen.findByText('SMTP relay');
    await user.click(screen.getByRole('button', { name: /^skip$/i }));
    expect(await screen.findByText('Create your admin account')).toBeInTheDocument();
  });

  it('admin step calls createFirstRunAdmin and stores session in auth store', async () => {
    const user = userEvent.setup();
    renderWizard();
    await screen.findByText('Welcome to JARVIS');
    await user.click(screen.getByRole('button', { name: /continue/i }));
    await screen.findByText('SMTP relay');
    await user.click(screen.getByRole('button', { name: /^skip$/i }));
    await screen.findByText('Create your admin account');

    await user.type(screen.getByLabelText(/admin email/i), 'admin@example.com');
    await user.click(screen.getByRole('button', { name: /create admin & sign in/i }));

    await waitFor(() => {
      // TanStack Query passes a context object as the second arg to mutationFn,
      // so we assert on the first arg only.
      expect(api.createFirstRunAdmin).toHaveBeenCalled();
      expect(vi.mocked(api.createFirstRunAdmin).mock.calls[0]?.[0]).toBe('admin@example.com');
    });

    // Auth store flipped to authed=true with admin user.
    await waitFor(() => {
      const state = useAuthStore.getState();
      expect(state.isAuthenticated).toBe(true);
      expect(state.user?.role).toBe('admin');
      expect(state.user?.email).toBe('admin@example.com');
    });

    // Should land on cloud LLM step next.
    expect(await screen.findByText(/Cloud LLM keys/i)).toBeInTheDocument();
  });
});
