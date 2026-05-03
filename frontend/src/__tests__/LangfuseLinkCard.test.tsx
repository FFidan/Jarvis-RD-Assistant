import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { LangfuseLinkCard } from '@/components/settings/LangfuseLinkCard';

// We need to control import.meta.env.VITE_LANGFUSE_PUBLIC_DASHBOARD between tests.
// Vitest exposes import.meta.env as a plain object so we can mutate it directly.

const ENV_KEY = 'VITE_LANGFUSE_PUBLIC_DASHBOARD';

afterEach(() => {
  // Clean up the env var after each test so module-level reads are consistent
  // when the module is re-imported. For these tests the component is a function
  // component that reads the module-level constant set at import time, so we
  // test two scenarios by using vi.resetModules() between them.
  vi.resetModules();
});

describe('LangfuseLinkCard', () => {
  it('renders nothing when VITE_LANGFUSE_PUBLIC_DASHBOARD is not set', async () => {
    // Ensure env var is absent
    delete (import.meta.env as Record<string, unknown>)[ENV_KEY];

    // Re-import the component so the module-level const is re-evaluated
    const { LangfuseLinkCard: Card } = await import(
      '@/components/settings/LangfuseLinkCard'
    );

    const { container } = render(<Card />);
    expect(container.firstChild).toBeNull();
  });

  it('renders a link to the dashboard when VITE_LANGFUSE_PUBLIC_DASHBOARD is set', async () => {
    const testUrl = 'https://cloud.langfuse.com/project/test-project';
    (import.meta.env as Record<string, unknown>)[ENV_KEY] = testUrl;

    const { LangfuseLinkCard: Card } = await import(
      '@/components/settings/LangfuseLinkCard'
    );

    render(<Card />);

    const link = screen.getByRole('link', { name: /Open Langfuse dashboard/i });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute('href', testUrl);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noreferrer noopener');

    // Clean up
    delete (import.meta.env as Record<string, unknown>)[ENV_KEY];
  });
});
