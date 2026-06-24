import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { CytoscapeGraph, type CytoscapeNode } from '@/components/graph/CytoscapeGraph';

// Capture what CytoscapeGraph passes to cytoscape() and the tap handler it binds.
interface CapturedCall {
  elements: Array<{ data: Record<string, unknown> }>;
}
const captured: { last: CapturedCall | null } = { last: null };
const tapHandlers: Array<(evt: { target: { id: () => string } }) => void> = [];

vi.mock('cytoscape', () => {
  const factory = vi.fn((cfg: { elements: CapturedCall['elements'] }) => {
    captured.last = { elements: cfg.elements };
    return {
      on: vi.fn((event: string, _selector: unknown, cb?: unknown) => {
        if (event === 'tap' && typeof cb === 'function') {
          tapHandlers.push(cb as (evt: { target: { id: () => string } }) => void);
        }
      }),
      fit: vi.fn(),
      destroy: vi.fn(),
    };
  });
  return { default: factory };
});

const NODES: CytoscapeNode[] = [
  { id: '1', label: 'Short', type: 'method', size: 20 },
  {
    id: '2',
    label: 'A really really long entity label that should be truncated',
    type: 'dataset',
    size: 20,
  },
];

describe('CytoscapeGraph', () => {
  beforeEach(() => {
    captured.last = null;
    tapHandlers.length = 0;
  });

  it('invokes onNodeClick with the tapped node id', () => {
    const onNodeClick = vi.fn();
    render(<CytoscapeGraph nodes={NODES} edges={[]} onNodeClick={onNodeClick} />);

    expect(tapHandlers.length).toBeGreaterThan(0);
    tapHandlers.forEach((cb) => cb({ target: { id: () => '2' } }));
    expect(onNodeClick).toHaveBeenCalledWith('2');
  });

  it('truncates long node labels while preserving the full label', () => {
    render(<CytoscapeGraph nodes={NODES} edges={[]} />);

    const nodeEls = captured.last!.elements.filter((e) => e.data.id === '2');
    expect(nodeEls).toHaveLength(1);
    const [nodeEl] = nodeEls;
    const label = nodeEl!.data.label as string;
    const fullLabel = nodeEl!.data.fullLabel as string;

    expect(label.length).toBeLessThanOrEqual(25);
    expect(label.endsWith('...')).toBe(true);
    expect(fullLabel).toBe('A really really long entity label that should be truncated');
  });

  it('does not truncate short labels', () => {
    render(<CytoscapeGraph nodes={NODES} edges={[]} />);
    const shortEl = captured.last!.elements.find((e) => e.data.id === '1');
    expect(shortEl!.data.label).toBe('Short');
  });
});
