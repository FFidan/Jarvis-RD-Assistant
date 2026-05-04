import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { ScoreStack } from '@/components/my-day/sections/ScoreStack';

describe('ScoreStack', () => {
  it('1-signal payload — emb segment fills 100%, others 0%', () => {
    const { container } = render(
      <ScoreStack score={0.8} parts={{ emb: 0.8, llm: 0, rec: 0, graph: 0 }} />,
    );

    // The bar container holds exactly 4 child divs (segment divs).
    // Each has an inline style with a width property.
    const segments = container.querySelectorAll<HTMLElement>('div[style*="width"]');
    expect(segments).toHaveLength(4);

    const [emb, llm, rec, graph] = Array.from(segments);

    // emb should take the full width (100%) since it's the only non-zero signal.
    expect(emb.style.width).toBe('100%');
    expect(emb.style.backgroundColor).toBe('var(--ink-blue)');

    // All other segments contribute 0%.
    expect(llm.style.width).toBe('0%');
    expect(rec.style.width).toBe('0%');
    expect(graph.style.width).toBe('0%');
  });

  it('4-signal payload — each segment is 25%', () => {
    const { container } = render(
      <ScoreStack score={0.5} parts={{ emb: 1, llm: 1, rec: 1, graph: 1 }} />,
    );

    const segments = container.querySelectorAll<HTMLElement>('div[style*="width"]');
    expect(segments).toHaveLength(4);

    const [emb, llm, rec, graph] = Array.from(segments);

    expect(emb.style.width).toBe('25%');
    expect(llm.style.width).toBe('25%');
    expect(rec.style.width).toBe('25%');
    expect(graph.style.width).toBe('25%');

    // Color checks — jsdom preserves the inline style string as-is for CSS custom
    // property values, so test via the style attribute string for robustness.
    expect(emb.style.backgroundColor).toBe('var(--ink-blue)');

    // For the fallback-colour CSS vars, check the inline attribute string contains
    // the expected value (jsdom may or may not normalise the var() expression).
    const llmBg = llm.getAttribute('style') ?? '';
    const recBg = rec.getAttribute('style') ?? '';
    const graphBg = graph.getAttribute('style') ?? '';

    expect(llmBg).toContain('#14b8a6');
    expect(recBg).toContain('#a855f7');
    expect(graphBg).toContain('#f59e0b');
  });

  it('title tooltips contain signal name and normalised percentage', () => {
    const { container } = render(
      <ScoreStack score={0.6} parts={{ emb: 0.5, llm: 0.5, rec: 0, graph: 0 }} />,
    );

    // sum = 1.0, so emb = 50%, llm = 50%, rec = 0%, graph = 0%
    const segments = container.querySelectorAll<HTMLElement>('div[style*="width"]');
    expect(segments).toHaveLength(4);

    const [emb, llm, rec, graph] = Array.from(segments);

    expect(emb.getAttribute('title')).toContain('emb 50%');
    expect(llm.getAttribute('title')).toContain('llm 50%');
    expect(rec.getAttribute('title')).toContain('rec 0%');
    expect(graph.getAttribute('title')).toContain('graph 0%');
  });

  it('zero-sum payload — renders a single gray "no signal" bar (not 4 equal blocks)', () => {
    const { container, getByTitle } = render(
      <ScoreStack score={0} parts={{ emb: 0, llm: 0, rec: 0, graph: 0 }} />,
    );

    // Should be exactly 1 segment div with style containing width
    const segments = container.querySelectorAll<HTMLElement>('div[style*="width"]');
    expect(segments).toHaveLength(1);

    const bar = getByTitle('no signal');
    expect(bar.style.width).toBe('100%');
  });

  it('zero-sum payload — badge label reads "no signal" not "emb·llm·rec·g"', () => {
    const { getByText } = render(
      <ScoreStack score={0} parts={{ emb: 0, llm: 0, rec: 0, graph: 0 }} />,
    );

    expect(getByText('no signal')).toBeInTheDocument();
  });
});
