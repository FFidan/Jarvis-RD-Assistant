import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ModelSelector } from '@/components/shared/ModelSelector';

// Mock Radix Select with native HTML elements (portals don't work in jsdom)
// Store the onValueChange callback so SelectItem can call it
let selectOnValueChange: ((value: string) => void) | undefined;

vi.mock('@/components/ui/select', () => ({
  Select: ({ children, value, onValueChange }: any) => {
    selectOnValueChange = onValueChange;
    return (
      <div data-testid="select-root" data-value={value}>
        {typeof children === 'function' ? children({ value }) : children}
      </div>
    );
  },
  SelectTrigger: ({ children }: any) => (
    <button data-testid="select-trigger">{children}</button>
  ),
  SelectValue: ({ placeholder }: any) => (
    <span data-testid="select-value">{placeholder}</span>
  ),
  SelectContent: ({ children }: any) => (
    <div data-testid="select-content">{children}</div>
  ),
  SelectItem: ({ children, value }: any) => (
    <div
      data-testid={`select-item-${value}`}
      onClick={() => selectOnValueChange?.(value)}
      role="option"
    >
      {children}
    </div>
  ),
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    apiFetch: vi.fn().mockResolvedValue({
      status: 'ok',
      installed: [
        { name: 'mistral-nemo', size: 4.1e9, parameter_size: '7B', quantization: 'Q4_0' },
        { name: 'qwen3.5:4b', size: 500e6, parameter_size: '4B', quantization: 'Q8_0' },
      ],
      hardware: { ollama_running: 1 },
      current: { smart_model: 'mistral-nemo' },
      issues: {},
    }),
  };
});

function renderComponent(props: Partial<React.ComponentProps<typeof ModelSelector>> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const defaultProps = {
    value: '',
    onChange: vi.fn(),
    ...props,
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <ModelSelector {...defaultProps} />
    </QueryClientProvider>,
  );
}

const defaultModels = {
  status: 'ok',
  installed: [
    { name: 'mistral-nemo', size: 4.1e9, parameter_size: '7B', quantization: 'Q4_0' },
    { name: 'qwen3.5:4b', size: 500e6, parameter_size: '4B', quantization: 'Q8_0' },
  ],
  hardware: { ollama_running: 1 },
  current: { smart_model: 'mistral-nemo' },
  issues: {},
};

describe('ModelSelector', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    // Restore default mock after tests that override it
    const { apiFetch } = await import('@/lib/api');
    vi.mocked(apiFetch).mockResolvedValue(defaultModels);
  });

  it('renders trigger with "Select a model" placeholder', () => {
    renderComponent();
    expect(screen.getByText('Select a model')).toBeInTheDocument();
  });

  it('shows model names with metadata (parameter_size, quantization, formatted size)', async () => {
    renderComponent();
    await waitFor(() => {
      expect(screen.getByText('mistral-nemo')).toBeInTheDocument();
      expect(screen.getByText('qwen3.5:4b')).toBeInTheDocument();
    });
    // Check metadata
    expect(screen.getByText('7B')).toBeInTheDocument();
    expect(screen.getByText('Q4_0')).toBeInTheDocument();
    expect(screen.getByText('4B')).toBeInTheDocument();
    expect(screen.getByText('Q8_0')).toBeInTheDocument();
    // Check formatted sizes
    expect(screen.getByText('(4.1GB)')).toBeInTheDocument();
    expect(screen.getByText('(500MB)')).toBeInTheDocument();
  });

  it('shows "current" badge when model matches role', async () => {
    renderComponent({ value: 'mistral-nemo', configKey: 'llm.smart_model' });
    await waitFor(() => {
      expect(screen.getByText('current')).toBeInTheDocument();
    });
    // "current" should only appear once (for mistral-nemo matching smart role)
    const currentBadges = screen.getAllByText('current');
    expect(currentBadges).toHaveLength(1);
  });

  it('shows "No models found. Is Ollama running?" when installed array is empty', async () => {
    const { apiFetch } = await import('@/lib/api');
    vi.mocked(apiFetch).mockResolvedValue({
      status: 'ok',
      installed: [],
      hardware: {},
      current: {},
      issues: {},
    });

    renderComponent();
    await waitFor(() => {
      expect(screen.getByText('No models found. Is Ollama running?')).toBeInTheDocument();
    });
  });

  it('shows degraded backend issue text instead of an empty-state guess', async () => {
    const { apiFetch } = await import('@/lib/api');
    vi.mocked(apiFetch).mockResolvedValue({
      status: 'degraded',
      installed: [],
      hardware: {},
      current: {},
      issues: { installed: 'Could not load installed Ollama models.' },
    });

    renderComponent();
    await waitFor(() => {
      expect(screen.getByText('Could not load installed Ollama models.')).toBeInTheDocument();
    });
  });

  it('shows query failures as errors instead of an empty-state message', async () => {
    const { apiFetch } = await import('@/lib/api');
    vi.mocked(apiFetch).mockRejectedValue(new Error('boom'));

    renderComponent();
    await waitFor(() => {
      expect(
        screen.getByText('Could not load models. Check the API and Ollama status.'),
      ).toBeInTheDocument();
    });
  });

  it('calls onChange with model name when item is selected', async () => {
    const onChange = vi.fn();
    renderComponent({ onChange });
    await waitFor(() => {
      expect(screen.getByText('mistral-nemo')).toBeInTheDocument();
    });

    // Click the SelectItem for mistral-nemo
    fireEvent.click(screen.getByTestId('select-item-mistral-nemo'));
    expect(onChange).toHaveBeenCalledWith('mistral-nemo');
  });

  it('formats sizes correctly: 4.1e9 → "(4.1GB)", 500e6 → "(500MB)"', async () => {
    renderComponent();
    await waitFor(() => {
      expect(screen.getByText('(4.1GB)')).toBeInTheDocument();
      expect(screen.getByText('(500MB)')).toBeInTheDocument();
    });
  });
});
