import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ExtractionTablePage } from '@/pages/ExtractionTablePage';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// Expose paper selection in tests without fighting the real combobox
vi.mock('@/components/shared/PaperSearchSelect', () => ({
  PaperSearchSelect: ({
    onChangeMulti,
  }: {
    values: number[];
    onChangeMulti: (ids: number[]) => void;
    placeholder?: string;
  }) => (
    <button data-testid="select-papers" onClick={() => onChangeMulti([1])}>
      Select papers
    </button>
  ),
}));

// Mock the API module
vi.mock('@/lib/api', async () => {
  const { createApiMock } = await import('@/__tests__/fixtures/api-mock');
  return createApiMock({
    fetchExtractionTemplates: async () => ([
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
    fetchPapersBrief: async () => ([
      { id: 1, title: 'Paper Alpha' },
      { id: 2, title: 'Paper Beta' },
    ]),
    fetchExtractionTable: async () => [],
    batchExtract: async () => ({ job_id: 'fake-id', total: 2 }),
  });
});

function renderPage() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <ExtractionTablePage />
    </MemoryRouter>,
    { queryClient },
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

  it('shows "Run extraction first" prompt when papers are selected but no extraction has run', async () => {
    renderPage();

    // Wait for template to load so the template ID is set
    await waitFor(() => {
      expect(screen.getByText(/Method, Dataset/)).toBeInTheDocument();
    });

    // Select papers via the mock button
    fireEvent.click(screen.getByTestId('select-papers'));

    // tableQuery is now enabled but fetchExtractionTable returns [] — no prior extraction
    await waitFor(() => {
      expect(screen.getByText('Run extraction first')).toBeInTheDocument();
      expect(
        screen.getByText('Run extraction first to generate data.'),
      ).toBeInTheDocument();
    });
  });

  it('shows "Extraction complete — no data found." after a successful but empty extraction', async () => {
    const { fetchExtractionTable } = await import('@/lib/api');
    (fetchExtractionTable as ReturnType<typeof vi.fn>).mockResolvedValue([]);

    renderPage();

    // Wait for template to load
    await waitFor(() => {
      expect(screen.getByText(/Method, Dataset/)).toBeInTheDocument();
    });

    // Select papers
    fireEvent.click(screen.getByTestId('select-papers'));

    // Wait until the "run extraction first" prompt is shown (pre-run state)
    await waitFor(() => {
      expect(screen.getByText('Run extraction first')).toBeInTheDocument();
    });

    // Click Extract Selected to trigger the mutation
    const extractButton = screen.getByText('Extract Selected');
    fireEvent.click(extractButton);

    // After mutation succeeds, empty result should show the post-run message
    await waitFor(() => {
      expect(screen.getByText('Extraction complete — no data found.')).toBeInTheDocument();
    });
  });

  it('shows an error message in the table card when the table query fails — not an empty state', async () => {
    const { fetchExtractionTable } = await import('@/lib/api');
    (fetchExtractionTable as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('Network failure'),
    );

    renderPage();

    // Wait for template to load
    await waitFor(() => {
      expect(screen.getByText(/Method, Dataset/)).toBeInTheDocument();
    });

    // Select papers to enable the table query
    fireEvent.click(screen.getByTestId('select-papers'));

    await waitFor(() => {
      expect(screen.getByText(/Failed to load extraction data/)).toBeInTheDocument();
    });

    // Must NOT show any empty-state copy when there's a real error
    expect(screen.queryByText('Run extraction first')).not.toBeInTheDocument();
    expect(screen.queryByText('Extraction complete — no data found.')).not.toBeInTheDocument();
    expect(screen.queryByText('Pick papers above and click Extract Selected to fill this table.')).not.toBeInTheDocument();
  });
});
