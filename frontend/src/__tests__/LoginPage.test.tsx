/**
 * Tests for LoginPage — FE-006: password input autoComplete attribute.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { LoginPage } from '@/pages/LoginPage';

// Mock auth-store so login() is a no-op during rendering
vi.mock('@/stores/auth-store', () => ({
  useAuthStore: () => ({
    login: vi.fn().mockResolvedValue(false),
  }),
}));

function renderLoginPage() {
  return render(
    <MemoryRouter>
      <LoginPage />
    </MemoryRouter>,
  );
}

describe('LoginPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the API key password input', () => {
    renderLoginPage();
    const input = screen.getByPlaceholderText(/Enter JARVIS_API_KEY/i);
    expect(input).toBeInTheDocument();
    expect(input).toHaveAttribute('type', 'password');
  });

  it('test_login_page_password_input_has_autocomplete_attr: password input has autoComplete="current-password"', () => {
    renderLoginPage();
    const input = screen.getByPlaceholderText(/Enter JARVIS_API_KEY/i);
    expect(input).toHaveAttribute('autocomplete', 'current-password');
  });
});
