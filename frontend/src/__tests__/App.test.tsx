import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { App } from '@/App';

// Mock fetch for auth store
vi.stubGlobal('fetch', vi.fn());

const { useAuthStore } = await import('@/stores/auth-store');

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('App', () => {
  it('shows login page when not authenticated', () => {
    useAuthStore.setState({ isAuthenticated: false, authTime: null, apiKey: null });
    renderApp();
    expect(screen.getByText('JARVIS RD Assistant')).toBeInTheDocument();
    expect(screen.getByLabelText('API Key')).toBeInTheDocument();
  });

  it('renders home for authenticated user', () => {
    useAuthStore.setState({ isAuthenticated: true, authTime: Date.now(), apiKey: 'test-key' });
    renderApp();
    // "Dashboard" appears in both TopBar and HomePage heading
    expect(screen.getAllByText('Dashboard').length).toBeGreaterThanOrEqual(1);
  });
});
