import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { ExtractionTemplateSection } from '@/components/settings/ExtractionTemplateSection';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchExtractionTemplates: vi.fn(),
    createExtractionTemplate: vi.fn().mockResolvedValue({}),
    updateExtractionTemplate: vi.fn().mockResolvedValue({}),
    deleteExtractionTemplate: vi.fn().mockResolvedValue({}),
  };
});

const { fetchExtractionTemplates, createExtractionTemplate } = await import('@/lib/api');

const mockTemplate = {
  id: 1,
  name: 'Method Comparison',
  description: 'Compare methods',
  fields: [{ name: 'method', label: 'Method', description: 'The method used', type: 'text' }],
  is_default: true,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

function renderSection() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <ExtractionTemplateSection />
    </MemoryRouter>,
    { queryClient },
  );
}

describe('ExtractionTemplateSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows "No extraction templates" empty state when no templates', async () => {
    vi.mocked(fetchExtractionTemplates).mockResolvedValue([]);
    renderSection();

    await waitFor(() => {
      expect(screen.getByText('No extraction templates')).toBeInTheDocument();
    });
  });

  it('renders template card with name, "Default" badge, and "1 fields" badge', async () => {
    vi.mocked(fetchExtractionTemplates).mockResolvedValue([mockTemplate]);
    renderSection();

    await waitFor(() => {
      expect(screen.getByText('Method Comparison')).toBeInTheDocument();
    });
    expect(screen.getByText('Default')).toBeInTheDocument();
    expect(screen.getByText('1 fields')).toBeInTheDocument();
  });

  it('clicking "Add Template" shows add form with "Template Name" label', async () => {
    const user = userEvent.setup();
    vi.mocked(fetchExtractionTemplates).mockResolvedValue([]);
    renderSection();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Add Template/ })).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /Add Template/ }));

    expect(screen.getByText('Template Name')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create Template' })).toBeInTheDocument();
  });

  it('clicking "Cancel" in add form hides it', async () => {
    const user = userEvent.setup();
    vi.mocked(fetchExtractionTemplates).mockResolvedValue([]);
    renderSection();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Add Template/ })).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /Add Template/ }));
    expect(screen.getByText('Template Name')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByText('Template Name')).not.toBeInTheDocument();
  });

  it('shows template description when provided', async () => {
    vi.mocked(fetchExtractionTemplates).mockResolvedValue([mockTemplate]);
    renderSection();

    await waitFor(() => {
      expect(screen.getByText('Compare methods')).toBeInTheDocument();
    });
  });

  it('shows field labels in template card', async () => {
    vi.mocked(fetchExtractionTemplates).mockResolvedValue([mockTemplate]);
    renderSection();

    await waitFor(() => {
      expect(screen.getByText('Method')).toBeInTheDocument();
    });
  });

  it('clicking "Create Template" calls createExtractionTemplate with parsed fields', async () => {
    const user = userEvent.setup();
    vi.mocked(fetchExtractionTemplates).mockResolvedValue([]);
    vi.mocked(createExtractionTemplate).mockResolvedValue({} as never);
    renderSection();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Add Template/ })).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /Add Template/ }));

    await user.type(screen.getByLabelText('Template Name'), 'New Template');
    await user.type(
      screen.getByRole('textbox', { name: /fields/i }),
      'accuracy|Accuracy|Model accuracy metric|text',
    );

    await user.click(screen.getByRole('button', { name: 'Create Template' }));

    await waitFor(() => {
      expect(vi.mocked(createExtractionTemplate)).toHaveBeenCalledWith(
        expect.objectContaining({
          name: 'New Template',
          fields: [
            { name: 'accuracy', label: 'Accuracy', description: 'Model accuracy metric', type: 'text' },
          ],
        }),
      );
    });
  });
});
