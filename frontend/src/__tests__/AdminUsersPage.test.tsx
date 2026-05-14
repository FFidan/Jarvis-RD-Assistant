/**
 * Tests for AdminUsersPage — Phase 2 WS-2B admin user management.
 *
 * Scope:
 * - Table renders users returned by listUsers.
 * - "Invite user" modal opens, submits, closes on success.
 * - Non-admin is redirected to "/" by AdminOnlyRoute.
 * - Error state shown when listUsers fails.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AdminUsersPage } from '@/pages/AdminUsersPage';
import { AdminOnlyRoute } from '@/components/auth/AdminOnlyRoute';
import { ApiError } from '@/lib/api';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const listUsersMock = vi.fn();
const inviteUserMock = vi.fn();
const updateUserRoleMock = vi.fn();
const deleteUserMock = vi.fn();

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    listUsers: () => listUsersMock(),
    inviteUser: (email: string, role: string) => inviteUserMock(email, role),
    updateUserRole: (userId: number, role: string) => updateUserRoleMock(userId, role),
    deleteUser: (userId: number) => deleteUserMock(userId),
  };
});

// Stable admin user for most tests; overridden per-test where needed.
let _mockRole: 'user' | 'admin' = 'admin';
let _mockUserId = 1;

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: (selector?: (s: { user: { id: number; email: string; role: 'user' | 'admin' } | null }) => unknown) => {
    const state = { user: { id: _mockUserId, email: 'admin@example.com', role: _mockRole } };
    return selector ? selector(state) : state;
  },
}));

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

const _sampleUsers = [
  {
    id: 1,
    email: 'admin@example.com',
    role: 'admin',
    created_at: new Date().toISOString(),
    last_login_at: null,
  },
  {
    id: 2,
    email: 'alice@example.com',
    role: 'user',
    created_at: new Date().toISOString(),
    last_login_at: null,
  },
];

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/admin/users']}>
        <Routes>
          <Route path="/admin/users" element={<AdminUsersPage />} />
          <Route path="/" element={<div>HOME</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderWithGuard(role: 'user' | 'admin') {
  _mockRole = role;
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/admin/users']}>
        <Routes>
          <Route
            path="/admin/users"
            element={
              <AdminOnlyRoute>
                <AdminUsersPage />
              </AdminOnlyRoute>
            }
          />
          <Route path="/" element={<div>HOME</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AdminUsersPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _mockRole = 'admin';
    _mockUserId = 1;
  });

  it('renders a table with users from the API', async () => {
    listUsersMock.mockResolvedValueOnce(_sampleUsers);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText('admin@example.com')).toBeInTheDocument();
    });
    expect(screen.getByText('alice@example.com')).toBeInTheDocument();
  });

  it('shows loading state initially', () => {
    listUsersMock.mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  it('shows error state when listUsers fails', async () => {
    listUsersMock.mockRejectedValueOnce(new ApiError(403, '{"detail":"Admin role required"}'));
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/failed to load users/i)).toBeInTheDocument();
    });
  });

  it('opens invite modal on button click', async () => {
    listUsersMock.mockResolvedValueOnce(_sampleUsers);
    renderPage();

    await waitFor(() => screen.getByText('admin@example.com'));

    const inviteBtn = screen.getByRole('button', { name: /invite user/i });
    await userEvent.click(inviteBtn);

    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByLabelText(/email address/i)).toBeInTheDocument();
  });

  it('invite modal submits and closes on success', async () => {
    listUsersMock.mockResolvedValue(_sampleUsers);
    inviteUserMock.mockResolvedValueOnce({
      id: 3,
      email: 'new@example.com',
      role: 'user',
      created_at: new Date().toISOString(),
      last_login_at: null,
    });

    renderPage();
    await waitFor(() => screen.getByText('admin@example.com'));

    await userEvent.click(screen.getByRole('button', { name: /invite user/i }));
    await userEvent.type(screen.getByLabelText(/email address/i), 'new@example.com');
    await userEvent.click(screen.getByRole('button', { name: /send invite/i }));

    await waitFor(() => {
      expect(inviteUserMock).toHaveBeenCalledWith('new@example.com', 'user');
    });
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
  });

  it('shows error in modal on invite failure', async () => {
    listUsersMock.mockResolvedValue(_sampleUsers);
    inviteUserMock.mockRejectedValueOnce(
      new ApiError(409, '{"detail":"A user with that email already exists"}'),
    );

    renderPage();
    await waitFor(() => screen.getByText('admin@example.com'));

    await userEvent.click(screen.getByRole('button', { name: /invite user/i }));
    await userEvent.type(screen.getByLabelText(/email address/i), 'admin@example.com');
    await userEvent.click(screen.getByRole('button', { name: /send invite/i }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/already exists/i);
    });
  });
});

describe('per-row role select isolation (DOM-F-07)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _mockRole = 'admin';
    _mockUserId = 1;
  });

  it('only disables the mutating row select; other rows remain enabled', async () => {
    // Mutation never resolves — stays pending so we can inspect disabled state.
    updateUserRoleMock.mockReturnValue(new Promise(() => {}));
    listUsersMock.mockResolvedValueOnce(_sampleUsers);

    renderPage();
    await waitFor(() => screen.getByText('alice@example.com'));

    const aliceTrigger = screen.getByRole('combobox', { name: /role for alice@example\.com/i });
    const adminTrigger = screen.getByRole('combobox', { name: /role for admin@example\.com/i });

    // Trigger mutation on alice's row.
    await userEvent.click(aliceTrigger);
    // Select an item from the open listbox.
    const adminOption = screen.getByRole('option', { name: /admin/i });
    await userEvent.click(adminOption);

    await waitFor(() => {
      expect(updateUserRoleMock).toHaveBeenCalled();
    });

    // alice's trigger must now be disabled; admin's trigger must still be enabled.
    expect(aliceTrigger).toBeDisabled();
    expect(adminTrigger).not.toBeDisabled();
  });
});

describe('AdminOnlyRoute guard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('redirects non-admin to "/" when accessing /admin/users', async () => {
    renderWithGuard('user');

    await waitFor(() => {
      expect(screen.getByText('HOME')).toBeInTheDocument();
    });
    expect(listUsersMock).not.toHaveBeenCalled();
  });

  it('renders admin page for admin role', async () => {
    listUsersMock.mockResolvedValueOnce(_sampleUsers);
    renderWithGuard('admin');

    await waitFor(() => {
      expect(screen.getByText('admin@example.com')).toBeInTheDocument();
    });
  });
});
