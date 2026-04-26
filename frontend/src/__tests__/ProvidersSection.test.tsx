import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { ProvidersSection } from '@/components/settings/ProvidersSection';

// --- Module mocks ---

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchConfig: vi.fn(),
    setConfig: vi.fn().mockResolvedValue({ key: '', value: null }),
    setProviderKey: vi.fn().mockResolvedValue(undefined),
    testProvider: vi.fn(),
    listJobs: vi.fn().mockResolvedValue([]),
    cancelJob: vi.fn(),
    getJob: vi.fn(),
  };
});

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: {
    getState: vi.fn(() => ({
      getApiKey: vi.fn(() => 'test-key'),
      logout: vi.fn(),
    })),
  },
}));

const { fetchConfig, setProviderKey, testProvider } = await import('@/lib/api');
const { toast } = await import('sonner');

const EMPTY_CONFIG: Array<{ key: string; value: unknown }> = [];

const MASKED_CONFIG = [
  { key: 'llm.anthropic.api_key', value: 'sk-ant-a****' },
  { key: 'llm.openai.api_key', value: 'sk-o****' },
  { key: 'llm.google.api_key', value: 'AIza****' },
];

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ProvidersSection />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

describe('ProvidersSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchConfig).mockResolvedValue(EMPTY_CONFIG);
  });

  it('renders three labeled inputs and three Test buttons', async () => {
    renderSection();
    // Use id-based queries to avoid ambiguity with the button aria-labels
    await waitFor(() => {
      expect(document.getElementById('provider-key-anthropic')).toBeInTheDocument();
      expect(document.getElementById('provider-key-openai')).toBeInTheDocument();
      expect(document.getElementById('provider-key-google')).toBeInTheDocument();
    });
    // Three visible labels
    expect(screen.getByText('Anthropic (Claude)')).toBeInTheDocument();
    expect(screen.getByText('OpenAI (GPT / o-series)')).toBeInTheDocument();
    expect(screen.getByText('Google (Gemini)')).toBeInTheDocument();
    // Three Test buttons
    const testButtons = screen.getAllByRole('button', { name: /test/i });
    expect(testButtons).toHaveLength(3);
  });

  it('displays masked values from backend in each input', async () => {
    vi.mocked(fetchConfig).mockResolvedValue(MASKED_CONFIG);
    renderSection();

    await waitFor(() => {
      const anthropicInput = document.getElementById('provider-key-anthropic') as HTMLInputElement;
      expect(anthropicInput).not.toBeNull();
      expect(anthropicInput.value).toBe('sk-ant-a****');

      const openaiInput = document.getElementById('provider-key-openai') as HTMLInputElement;
      expect(openaiInput).not.toBeNull();
      expect(openaiInput.value).toBe('sk-o****');

      const googleInput = document.getElementById('provider-key-google') as HTMLInputElement;
      expect(googleInput).not.toBeNull();
      expect(googleInput.value).toBe('AIza****');
    });
    expect(screen.getAllByText('Configured, not tested')).toHaveLength(3);
  });

  it('calls setProviderKey with correct provider on blur when value changed', async () => {
    const user = userEvent.setup();
    renderSection();

    // Wait for component to render (loading state resolves)
    await waitFor(() => {
      expect(document.getElementById('provider-key-anthropic')).toBeInTheDocument();
    });
    const anthropicInput = document.getElementById('provider-key-anthropic') as HTMLInputElement;
    await user.click(anthropicInput);
    await user.type(anthropicInput, 'sk-ant-new-key');
    await user.tab(); // trigger blur

    await waitFor(() => {
      expect(vi.mocked(setProviderKey)).toHaveBeenCalledWith(
        'anthropic',
        expect.stringContaining('sk-ant-new-key'),
      );
    });
  });

  it('does NOT call setProviderKey on blur when value is unchanged (masked)', async () => {
    vi.mocked(fetchConfig).mockResolvedValue(MASKED_CONFIG);
    const user = userEvent.setup();
    renderSection();

    await waitFor(() => {
      expect(document.getElementById('provider-key-anthropic')).toBeInTheDocument();
    });
    const anthropicInput = document.getElementById('provider-key-anthropic') as HTMLInputElement;
    // Focus then blur without changing value
    await user.click(anthropicInput);
    await user.tab();

    await waitFor(() => {
      expect(vi.mocked(setProviderKey)).not.toHaveBeenCalled();
    });
  });

  it('calls testProvider with correct provider on Test button click', async () => {
    vi.mocked(testProvider).mockResolvedValue({ ok: true, error: null });
    const user = userEvent.setup();
    renderSection();

    await waitFor(() => screen.getAllByRole('button', { name: /test/i }));
    const [anthropicTestBtn] = screen.getAllByRole('button', { name: /test/i });
    await user.click(anthropicTestBtn);

    await waitFor(() => {
      expect(vi.mocked(testProvider)).toHaveBeenCalledWith('anthropic');
    });
  });

  it('shows success toast when testProvider returns ok=true', async () => {
    vi.mocked(testProvider).mockResolvedValue({ ok: true, error: null });
    const user = userEvent.setup();
    renderSection();

    await waitFor(() => screen.getAllByRole('button', { name: /test/i }));
    const [anthropicTestBtn] = screen.getAllByRole('button', { name: /test/i });
    await user.click(anthropicTestBtn);

    await waitFor(() => {
      expect(vi.mocked(toast.success)).toHaveBeenCalledWith(
        expect.stringContaining('connection OK'),
      );
    });
  });

  it('shows error toast when testProvider returns ok=false', async () => {
    vi.mocked(testProvider).mockResolvedValue({ ok: false, error: 'Invalid API key' });
    const user = userEvent.setup();
    renderSection();

    await waitFor(() => screen.getAllByRole('button', { name: /test/i }));
    const [anthropicTestBtn] = screen.getAllByRole('button', { name: /test/i });
    await user.click(anthropicTestBtn);

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Invalid API key');
    });
    expect(screen.getByText('Configured, degraded: Invalid API key')).toBeInTheDocument();
  });

  it('shows not configured status for blank providers', async () => {
    renderSection();

    await waitFor(() => {
      expect(screen.getAllByText('Not configured')).toHaveLength(3);
    });
  });

  it('shows error toast when testProvider throws', async () => {
    vi.mocked(testProvider).mockRejectedValue(new Error('Network error'));
    const user = userEvent.setup();
    renderSection();

    await waitFor(() => screen.getAllByRole('button', { name: /test/i }));
    const [anthropicTestBtn] = screen.getAllByRole('button', { name: /test/i });
    await user.click(anthropicTestBtn);

    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Network error');
    });
  });

  it('disables Test button while test is in flight', async () => {
    let resolveTest!: (v: { ok: boolean; error: string | null }) => void;
    vi.mocked(testProvider).mockImplementation(
      () =>
        new Promise((res) => {
          resolveTest = res;
        }),
    );

    const user = userEvent.setup();
    renderSection();

    await waitFor(() => screen.getAllByRole('button', { name: /test/i }));
    const [anthropicTestBtn] = screen.getAllByRole('button', { name: /test/i });
    await user.click(anthropicTestBtn);

    // Button should be disabled while in flight
    await waitFor(() => {
      expect(anthropicTestBtn).toBeDisabled();
    });

    // Resolve and button should re-enable
    resolveTest({ ok: true, error: null });
    await waitFor(() => {
      expect(anthropicTestBtn).not.toBeDisabled();
    });
  });
});
