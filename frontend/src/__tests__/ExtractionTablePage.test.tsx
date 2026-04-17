import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { ExtractionTablePage } from '@/pages/ExtractionTablePage';

// Mock the API module
vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchExtractionTemplates: vi.fn().mockResolvedValue([
      {
        id: 1,
        name: 'Method Comparison',
        description: 'Compare methods across papers',
        fields: [
          { name: 'method', label: 'Method', description: 'The method used', type: 'text' },
          { name: 'dataset', label: 'Dataset', description: 'Evaluation dataset', type: 'text' },
        ],
        is_default: true,
        created_at: '2026-03-01T00:00:00Z',
        updated_at: '2026-03-01T00:00:00Z',
      },
    ]),
    fetchPapersBrief: vi.fn().mockResolvedValue([
      { id: 1, title: 'Paper Alpha' },
      { id: 2, title: 'Paper Beta' },
    ]),
    fetchExtractionTable: vi.fn().mockResolvedValue([]),
    batchExtract: vi.fn().mockResolvedValue({ job_id: 'fake-id', total: 2 }),
  };
});

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ExtractionTablePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ExtractionTablePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the page title', () => {
    renderPage();
    expect(screen.getByText('Extraction Table')).toBeInTheDocument();
  });

  it('renders the template selector after loading', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('Extraction Template')).toBeInTheDocument();
    });
  });

  it('renders the paper selection section', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Paper Selection/)).toBeInTheDocument();
    });
  });

  it('shows empty state for table when no papers selected', async () => {
    renderPage();
    await waitFor(() => {
      expect(
        screen.getByText('Pick papers above and click Extract Selected to fill this table.'),
      ).toBeInTheDocument();
    });
  });

  it('renders Extract Selected button', () => {
    renderPage();
    expect(screen.getByText('Extract Selected')).toBeInTheDocument();
  });

  it('shows field labels from the selected template', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Method, Dataset/)).toBeInTheDocument();
    });
  });

  it('does not emit the select controlled-state warning during auto-select', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const consoleWarn = vi.spyOn(console, 'warn').mockImplementation(() => {});

    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/Method, Dataset/)).toBeInTheDocument();
    });

    expect(
      consoleError.mock.calls.some(([message]) =>
        String(message).includes('A component is changing an uncontrolled input to be controlled') ||
        String(message).includes('Select is changing from uncontrolled to controlled'),
      ),
    ).toBe(false);
    expect(
      consoleWarn.mock.calls.some(([message]) =>
        String(message).includes('Select is changing from uncontrolled to controlled') ||
        String(message).includes('A form field element should have an id or name attribute'),
      ),
    ).toBe(false);

    consoleError.mockRestore();
    consoleWarn.mockRestore();
  });
});
