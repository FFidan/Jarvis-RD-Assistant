import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ConfigEntryCard } from '@/components/settings/ingestion/ConfigEntryCard';
import type { ConfigEntry } from '@/types';
import type { ConfigEntryMeta } from '@/components/settings/ingestion/ConfigEntryCard';

const baseProps = {
  entry: { key: 'sample.key', value: 'hello' } as ConfigEntry,
  meta: undefined as ConfigEntryMeta | undefined,
  editingKey: null as string | null,
  editValue: '',
  saveError: null as string | null,
  isMutPending: false,
  onMutate: vi.fn(),
  onStartEdit: vi.fn(),
  onEditValueChange: vi.fn(),
  onSaveEdit: vi.fn(),
  onCancelEdit: vi.fn(),
};

describe('ConfigEntryCard', () => {
  it('renders customElement when prop provided (overrides default branches)', () => {
    render(<ConfigEntryCard {...baseProps} customElement={<div data-testid="cu">CUSTOM</div>} />);
    expect(screen.getByTestId('cu')).toBeInTheDocument();
  });

  it('renders Switch when meta.type === boolean', () => {
    const meta: ConfigEntryMeta = {
      type: 'boolean',
      label: 'Toggle Feature',
      description: 'Enables the feature',
      group: 'general',
    };
    render(
      <ConfigEntryCard
        {...baseProps}
        meta={meta}
        entry={{ key: 'feature.enabled', value: 'true' } as ConfigEntry}
      />,
    );
    expect(screen.getByRole('switch', { name: 'Toggle Feature' })).toBeInTheDocument();
  });

  it('renders Input + Save/Cancel buttons when editingKey === entry.key', () => {
    render(<ConfigEntryCard {...baseProps} editingKey="sample.key" editValue="hello" />);
    expect(screen.getByRole('textbox')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save setting/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /cancel edit/i })).toBeInTheDocument();
  });

  it('renders saveError text under input when saveError prop set', () => {
    render(
      <ConfigEntryCard
        {...baseProps}
        editingKey="sample.key"
        editValue="bad"
        saveError="Value must be > 0"
      />,
    );
    expect(screen.getByText(/Value must be > 0/)).toBeInTheDocument();
  });

  it('renders view-mode with Pencil button when not editing', () => {
    render(<ConfigEntryCard {...baseProps} />);
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /edit setting/i })).toBeInTheDocument();
  });

  it('renders saveError alongside customElement (model cards surface failures)', () => {
    render(
      <ConfigEntryCard
        {...baseProps}
        customElement={<div data-testid="cu">CUSTOM</div>}
        saveError="Failed to save: HTTP 400"
      />,
    );
    expect(screen.getByTestId('cu')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Failed to save: HTTP 400');
    // Keyed testid lets section tests assert the error paints under ONE card.
    expect(screen.getByTestId('config-save-error-sample.key')).toBeInTheDocument();
  });

  it('does NOT render an error element for customElement when saveError is null', () => {
    render(<ConfigEntryCard {...baseProps} customElement={<div data-testid="cu">CUSTOM</div>} />);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
