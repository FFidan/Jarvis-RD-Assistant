import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { resetAuthState } from '@/__tests__/auth-test-utils';
import { LogsRoute } from '@/components/auth/LogsRoute';
import { useAuthStore } from '@/stores/auth-store';

function renderGuard(children = <div>logs content</div>) {
  return render(
    <MemoryRouter>
      <LogsRoute>{children}</LogsRoute>
    </MemoryRouter>,
  );
}

describe('LogsRoute', () => {
  it('renders children for an admin user', () => {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      user: { id: 1, email: 'admin@example.com', role: 'admin' },
    });
    renderGuard();
    expect(screen.getByText('logs content')).toBeInTheDocument();
  });

  it('redirects a member user to /', () => {
    useAuthStore.setState({
      isAuthenticated: true,
      authTime: Date.now(),
      user: { id: 2, email: 'member@example.com', role: 'user' },
    });
    renderGuard();
    expect(screen.queryByText('logs content')).not.toBeInTheDocument();
  });

  it('redirects when not authenticated', () => {
    resetAuthState();
    renderGuard();
    expect(screen.queryByText('logs content')).not.toBeInTheDocument();
  });
});
