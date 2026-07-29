import { screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { LangfuseLinkCard } from '@/components/settings/LangfuseLinkCard';
import { useAuthStore } from '@/stores/auth-store';
import type { ConfigEntry } from '@/types';

vi.mock('@/lib/api', () => ({
  fetchConfig: vi.fn(),
  setConfig: vi.fn(),
}));

import { fetchConfig, setConfig } from '@/lib/api';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

const CONFIG_KEY = 'observability.langfuse_dashboard_url';

const asMock = (fn: unknown) => fn as ReturnType<typeof vi.fn>;

const wrap = (ui: React.ReactNode) => {
  const qc = createTestQueryClient();
  return renderWithProviders(
    ui,
    { queryClient: qc },
  );
};

const setUser = (role: 'user' | 'admin') =>
  useAuthStore.setState({ user: { id: 1, email: 'a@b.c', role } });

const config = (value: string): ConfigEntry[] => [{ key: CONFIG_KEY, value }];

describe('LangfuseLinkCard (webapp-configured)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    asMock(setConfig).mockResolvedValue({ key: CONFIG_KEY, value: '' });
  });

  it('shows an admin URL input when nothing is configured', async () => {
    setUser('admin');
    asMock(fetchConfig).mockResolvedValue([]);
    wrap(<LangfuseLinkCard />);

    await waitFor(() =>
      expect(screen.getByLabelText(/Langfuse dashboard URL/i)).toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: /Save/i })).toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: /Open Langfuse dashboard/i }),
    ).not.toBeInTheDocument();
  });

  it('renders an external link when a safe URL is configured', async () => {
    setUser('admin');
    const url = 'https://cloud.langfuse.com/project/abc';
    asMock(fetchConfig).mockResolvedValue(config(url));
    wrap(<LangfuseLinkCard />);

    const link = await screen.findByRole('link', { name: /Open Langfuse dashboard/i });
    expect(link).toHaveAttribute('href', url);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noreferrer noopener');
  });

  it('accepts a local http://localhost dashboard URL as safe', async () => {
    setUser('admin');
    asMock(fetchConfig).mockResolvedValue(config('http://localhost:3002'));
    wrap(<LangfuseLinkCard />);

    const link = await screen.findByRole('link', { name: /Open Langfuse dashboard/i });
    expect(link).toHaveAttribute('href', 'http://localhost:3002');
  });

  it('tells a non-admin it is unconfigured without an input', async () => {
    setUser('user');
    asMock(fetchConfig).mockResolvedValue([]);
    wrap(<LangfuseLinkCard />);

    await waitFor(() =>
      expect(screen.getByText(/ask an administrator/i)).toBeInTheDocument(),
    );
    expect(screen.queryByLabelText(/Langfuse dashboard URL/i)).not.toBeInTheDocument();
  });

  it('blocks an unsafe URL client-side and does not call setConfig', async () => {
    setUser('admin');
    asMock(fetchConfig).mockResolvedValue([]);
    wrap(<LangfuseLinkCard />);

    const input = await screen.findByLabelText(/Langfuse dashboard URL/i);
    fireEvent.change(input, { target: { value: 'http://evil.example.com' } });
    fireEvent.click(screen.getByRole('button', { name: /Save/i }));

    await waitFor(() => expect(screen.getByText(/https:\/\//i)).toBeInTheDocument());
    expect(setConfig).not.toHaveBeenCalled();
  });

  it('saves a valid URL via setConfig', async () => {
    setUser('admin');
    asMock(fetchConfig).mockResolvedValue([]);
    wrap(<LangfuseLinkCard />);

    const input = await screen.findByLabelText(/Langfuse dashboard URL/i);
    fireEvent.change(input, { target: { value: 'https://langfuse.example.com' } });
    fireEvent.click(screen.getByRole('button', { name: /Save/i }));

    await waitFor(() =>
      expect(setConfig).toHaveBeenCalledWith(CONFIG_KEY, 'https://langfuse.example.com'),
    );
  });
});
