import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { screen, waitFor, act, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { KnowledgeGraphPage } from '@/pages/KnowledgeGraphPage';
import { useAuthStore } from '@/stores/auth-store';
import * as api from '@/lib/api';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// Radix UI Select uses pointer-capture and scrollIntoView APIs not present in jsdom.
beforeAll(() => {
  if (!window.HTMLElement.prototype.hasPointerCapture) {
    window.HTMLElement.prototype.hasPointerCapture = () => false;
  }
  if (!window.HTMLElement.prototype.setPointerCapture) {
    window.HTMLElement.prototype.setPointerCapture = () => {};
  }
  if (!window.HTMLElement.prototype.releasePointerCapture) {
    window.HTMLElement.prototype.releasePointerCapture = () => {};
  }
  if (!window.HTMLElement.prototype.scrollIntoView) {
    window.HTMLElement.prototype.scrollIntoView = () => {};
  }
});

// Mock cytoscape so jsdom doesn't choke on canvas. Capture registered tap
// handlers so a test can simulate a node click.
const tapHandlers: Array<(evt: { target: { id: () => string } }) => void> = [];
vi.mock('cytoscape', () => {
  const mockCy = {
    on: vi.fn((event: string, selectorOrCb: unknown, cb?: unknown) => {
      if (event === 'tap' && typeof cb === 'function') {
        tapHandlers.push(cb as (evt: { target: { id: () => string } }) => void);
      }
    }),
    fit: vi.fn(),
    destroy: vi.fn(),
  };
  return { default: vi.fn(() => mockCy) };
});

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    getKnowledgeGraph: vi.fn().mockResolvedValue({
      entities: [
        { id: 1, name: 'Transformer', canonical_name: 'transformer', entity_type: 'method', description: 'Self-attention architecture', metadata: {}, paper_count: 12, created_at: '2026-01-01T00:00:00Z', display_size: 40 },
        { id: 2, name: 'ImageNet', canonical_name: 'imagenet', entity_type: 'dataset', description: 'Large image dataset', metadata: {}, paper_count: 8, created_at: '2026-01-01T00:00:00Z', display_size: 39 },
        { id: 3, name: 'BLEU', canonical_name: 'bleu', entity_type: 'metric', description: 'Translation quality metric', metadata: {}, paper_count: 5, created_at: '2026-01-01T00:00:00Z', display_size: 30 },
      ],
      relationships: [
        { id: 1, source_entity_id: 1, target_entity_id: 2, relationship_type: 'evaluated_on', paper_id: 1, evidence_quote: null, confidence: 0.9, created_at: '2026-01-01T00:00:00Z' },
        { id: 2, source_entity_id: 1, target_entity_id: 3, relationship_type: 'measured_by', paper_id: 1, evidence_quote: null, confidence: 0.85, created_at: '2026-01-01T00:00:00Z' },
      ],
      entity_type_counts: { method: 1, dataset: 1, metric: 1 },
    }),
    queryKnowledgeGraph: vi.fn().mockResolvedValue({
      results: [{ answer: 'Transformer is a method used on ImageNet' }],
    }),
    batchExtractEntities: vi.fn().mockResolvedValue({ extracted: 0 }),
  };
});

const WITH_ENTITIES = {
  entities: [
    { id: 1, name: 'Transformer', canonical_name: 'transformer', entity_type: 'method', description: 'Self-attention architecture', metadata: {}, paper_count: 12, created_at: '2026-01-01T00:00:00Z', display_size: 40 },
    { id: 2, name: 'ImageNet', canonical_name: 'imagenet', entity_type: 'dataset', description: 'Large image dataset', metadata: {}, paper_count: 8, created_at: '2026-01-01T00:00:00Z', display_size: 39 },
    { id: 3, name: 'BLEU', canonical_name: 'bleu', entity_type: 'metric', description: 'Translation quality metric', metadata: {}, paper_count: 5, created_at: '2026-01-01T00:00:00Z', display_size: 30 },
  ],
  relationships: [
    { id: 1, source_entity_id: 1, target_entity_id: 2, relationship_type: 'evaluated_on', paper_id: 1, evidence_quote: null, confidence: 0.9, created_at: '2026-01-01T00:00:00Z' },
    { id: 2, source_entity_id: 1, target_entity_id: 3, relationship_type: 'measured_by', paper_id: 1, evidence_quote: null, confidence: 0.85, created_at: '2026-01-01T00:00:00Z' },
  ],
  entity_type_counts: { method: 1, dataset: 1, metric: 1 },
};

const EMPTY_GRAPH = { entities: [], relationships: [], entity_type_counts: {} };

function renderPage() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter>
      <KnowledgeGraphPage />
    </MemoryRouter>,
    { queryClient },
  );
}

describe('KnowledgeGraphPage', () => {
  beforeEach(() => {
    tapHandlers.length = 0;
    vi.mocked(api.getKnowledgeGraph).mockResolvedValue(WITH_ENTITIES);
  });

  it('renders the page title', () => {
    renderPage();
    expect(screen.getByText('Knowledge Graph')).toBeInTheDocument();
  });

  it('renders entity type filter', () => {
    renderPage();
    expect(screen.getByText('Entity Type')).toBeInTheDocument();
  });

  it('renders min paper count slider', () => {
    renderPage();
    expect(screen.getByText(/Min Paper Count:/)).toBeInTheDocument();
  });

  it('renders layout selector', () => {
    renderPage();
    expect(screen.getByText('Layout:')).toBeInTheDocument();
  });

  it('renders query input', () => {
    renderPage();
    expect(
      screen.getByPlaceholderText("Query (e.g., 'What methods are used on ImageNet?')"),
    ).toBeInTheDocument();
  });

  it('shows graph stats after data loads', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('Total Entities')).toBeInTheDocument();
      expect(screen.getByText('Total Relationships')).toBeInTheDocument();
      expect(screen.getByText('Entity Types')).toBeInTheDocument();
    });
  });

  it('shows entity breakdown after data loads', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('Entities by Type')).toBeInTheDocument();
      expect(screen.getByText('method: 1')).toBeInTheDocument();
      expect(screen.getByText('dataset: 1')).toBeInTheDocument();
      expect(screen.getByText('metric: 1')).toBeInTheDocument();
    });
  });

  it('shows cytoscape container when data is available', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('cytoscape-container')).toBeInTheDocument();
    });
  });

  it('renders the Query button', () => {
    renderPage();
    expect(screen.getByText('Query')).toBeInTheDocument();
  });

  it('shows total relationships stat', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('Total Relationships')).toBeInTheDocument();
      expect(screen.getByText('2')).toBeInTheDocument(); // 2 relationships
    });
  });
});

describe('KnowledgeGraphPage — node detail panel', () => {
  beforeEach(() => {
    tapHandlers.length = 0;
    vi.mocked(api.getKnowledgeGraph).mockResolvedValue(WITH_ENTITIES);
  });

  it('shows a degraded empty state before any node is selected', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('kg-node-detail')).toBeInTheDocument();
    });
    expect(
      screen.getByText(/Click a node in the graph to see its details/i),
    ).toBeInTheDocument();
  });

  it('opens the panel with the clicked node’s details and relationships', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('cytoscape-container')).toBeInTheDocument();
    });
    // Cytoscape registered at least one tap('node', cb) handler.
    expect(tapHandlers.length).toBeGreaterThan(0);

    // Simulate clicking entity id 1 (Transformer).
    act(() => {
      tapHandlers.forEach((cb) => cb({ target: { id: () => '1' } }));
    });

    const panel = screen.getByTestId('kg-node-detail');
    expect(panel).toHaveTextContent('Transformer');
    expect(panel).toHaveTextContent('method'); // entity_type badge
    expect(panel).toHaveTextContent('Self-attention architecture'); // description
    expect(panel).toHaveTextContent('Relationships (2)');
    // Connected entity names resolved from ids.
    expect(panel).toHaveTextContent('ImageNet');
    expect(panel).toHaveTextContent('BLEU');
  });
});

describe('KnowledgeGraphPage — Batch Extract admin gate', () => {
  beforeEach(() => {
    vi.mocked(api.getKnowledgeGraph).mockResolvedValue(EMPTY_GRAPH);
  });

  function renderAsRole(role: 'user' | 'admin') {
    useAuthStore.setState({ user: { id: 1, email: 'a@b.c', role } });
    const queryClient = createTestQueryClient();
    return renderWithProviders(
      <MemoryRouter>
        <KnowledgeGraphPage />
      </MemoryRouter>,
      { queryClient },
    );
  }

  it('hides "Batch Extract Entities" button from non-admin users', async () => {
    renderAsRole('user');
    await waitFor(() => {
      expect(screen.getByText('No entities extracted yet')).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /Batch Extract Entities/i })).not.toBeInTheDocument();
  });

  it('shows "Batch Extract Entities" button to admin users', async () => {
    renderAsRole('admin');
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Batch Extract Entities/i })).toBeInTheDocument();
    });
  });

  it('hides "Batch Extract Entities" from admins once entities exist', async () => {
    vi.mocked(api.getKnowledgeGraph).mockResolvedValue(WITH_ENTITIES);
    renderAsRole('admin');
    await waitFor(() => {
      expect(screen.getByTestId('cytoscape-container')).toBeInTheDocument();
    });
    expect(
      screen.queryByRole('button', { name: /Batch Extract Entities/i }),
    ).not.toBeInTheDocument();
  });
});

describe('KnowledgeGraphPage — Reset filters', () => {
  beforeEach(() => {
    tapHandlers.length = 0;
    vi.mocked(api.getKnowledgeGraph).mockClear();
    vi.mocked(api.getKnowledgeGraph).mockResolvedValue(WITH_ENTITIES);
  });

  it('drives the server query when the Min Paper Count slider moves', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('cytoscape-container')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByRole('slider'), { target: { value: '4' } });

    await waitFor(() => {
      expect(vi.mocked(api.getKnowledgeGraph)).toHaveBeenLastCalledWith(undefined, 4);
    });
  });

  it('reverts entity type, paper-count threshold and node selection to their defaults', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('cytoscape-container')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByRole('slider'), { target: { value: '4' } });
    expect(screen.getByText('Min Paper Count: 4')).toBeInTheDocument();

    // Pick an entity type other than "All" (the first combobox — EntityTypeFilter
    // precedes the layout selector in the Filters card).
    const [entityTypeSelect] = screen.getAllByRole('combobox');
    expect(entityTypeSelect).toBeDefined();
    await user.click(entityTypeSelect as HTMLElement);
    await user.click(await screen.findByRole('option', { name: 'Method' }));
    await waitFor(() => {
      expect(vi.mocked(api.getKnowledgeGraph)).toHaveBeenLastCalledWith('method', 4);
    });

    expect(tapHandlers.length).toBeGreaterThan(0);
    act(() => {
      tapHandlers.forEach((cb) => cb({ target: { id: () => '1' } }));
    });
    expect(screen.getByTestId('kg-node-detail')).toHaveTextContent('Transformer');

    await user.click(screen.getByRole('button', { name: 'Reset filters' }));

    expect(screen.getByText('Min Paper Count: 1')).toBeInTheDocument();
    expect(
      screen.getByText('Click a node in the graph to see its details and relationships.'),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(vi.mocked(api.getKnowledgeGraph)).toHaveBeenLastCalledWith(undefined, 1);
    });
  });
});
