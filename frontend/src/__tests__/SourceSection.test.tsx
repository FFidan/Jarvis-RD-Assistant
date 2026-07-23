import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { DndContext } from '@dnd-kit/core';
import { SortableContext } from '@dnd-kit/sortable';
import { SourceSection } from '@/components/settings/SourceSection';
import type { SourceConfig } from '@/types';

vi.mock('@/lib/api', () => ({
  updateSource: vi.fn().mockResolvedValue({}),
}));

function source(overrides: Partial<SourceConfig> = {}): SourceConfig {
  return {
    id: 1,
    source_type: 'pubmed',
    enabled: true,
    config: { key_env: 'PUBMED_API_KEY', requires_key: false },
    priority: 1,
    display_order: 1,
    created_at: '2026-05-06T00:00:00Z',
    ...overrides,
  };
}

function renderSource(row: SourceConfig) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <DndContext>
        <SortableContext items={[row.source_type]}>
          <SourceSection source={row} displayIdx={1} />
        </SortableContext>
      </DndContext>
    </QueryClientProvider>,
  );
}

describe('SourceSection', () => {
  it('shows optional-key copy for enabled PubMed without an API key', () => {
    renderSource(source());

    expect(screen.getByText(/API key: optional/i)).toBeInTheDocument();
    expect(screen.getByText(/standard rate limit/i)).toBeInTheDocument();
    expect(screen.queryByText(/^API key: not set$/i)).not.toBeInTheDocument();
  });

  it('keeps required-key warning for enabled OpenAlex without an API key', () => {
    renderSource(
      source({
        source_type: 'openalex',
        config: { key_env: 'OPENALEX_API_KEY', requires_key: true },
      }),
    );

    expect(screen.getByText(/^API key: not set$/i)).toBeInTheDocument();
  });
});
