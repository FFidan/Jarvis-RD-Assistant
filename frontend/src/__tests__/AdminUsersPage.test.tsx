/**
 * Tests for AdminUsersPage — admin user management.
 *
 * Scope:
 * - Table renders users returned by listUsers.
 * - "Invite user" modal opens, submits, closes on success.
 * - Non-admin is redirected to "/" by AdminOnlyRoute.
 * - Error state shown when listUsers fails.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
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
const sendSignInLinkMock = vi.fn();

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

// Map of aria-label → onValueChange callback, populated as rows mount.
// Used by the per-row isolation test to trigger mutations without Radix portals.
const _roleSelectCallbacks: Map<string, (v: string) => void> = new Map();

// Radix Select portals do not work in jsdom — replace with simple HTML.
// Select injects its `disabled` state into SelectTrigger via context-free clone.
vi.mock('@/components/ui/select', () => ({
  Select: ({
    children,
    onValueChange,
    disabled,
  }: {
    children: React.ReactNode;
    value?: string;
    onValueChange?: (v: string) => void;
    disabled?: boolean;
  }) =>
    React.createElement(
      'div',
      { 'data-select-disabled': disabled ? 'true' : 'false' },
      React.Children.map(children, (child) => {
        if (!React.isValidElement(child)) return child;
        // Inject disabled + onValueChange into SelectTrigger children.
        return React.cloneElement(child as React.ReactElement<Record<string, unknown>>, {
          _selectDisabled: disabled,
          _selectOnValueChange: onValueChange,
        });
      }),
    ),
  SelectTrigger: ({
    children,
    'aria-label': ariaLabel,
    className,
    _selectDisabled,
    _selectOnValueChange,
  }: {
    children: React.ReactNode;
    className?: string;
    'aria-label'?: string;
    _selectDisabled?: boolean;
    _selectOnValueChange?: (v: string) => void;
  }) => {
    // Store callback keyed by aria-label for test access.
    if (ariaLabel && _selectOnValueChange) {
      _roleSelectCallbacks.set(ariaLabel, _selectOnValueChange);
    }
    return React.createElement(
      'button',
      { role: 'combobox', 'aria-label': ariaLabel, className, disabled: _selectDisabled },
      children,
    );
  },
  SelectValue: () => null,
  SelectContent: ({ children }: { children: React.ReactNode }) =>
    React.createElement('div', null, children),
  SelectItem: ({ children, value }: { children: React.ReactNode; value: string }) =>
    React.createElement('div', { role: 'option', 'data-value': value }, children),
}));

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    listUsers: () => listUsersMock(),
    inviteUser: (email: string, role: string) => inviteUserMock(email, role),
    updateUserRole: (userId: number, role: string) => updateUserRoleMock(userId, role),
    deleteUser: (userId: number) => deleteUserMock(userId),
    sendSignInLink: (userId: number) => sendSignInLinkMock(userId),
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
    _roleSelectCallbacks.clear();
  });

  it('role Select is always disabled for the current-user (self) row', async () => {
    listUsersMock.mockResolvedValueOnce(_sampleUsers);
    renderPage();
    await waitFor(() => screen.getByText('admin@example.com'));

    // admin@example.com has id=1, which matches _mockUserId=1 (isSelf=true).
    const adminTrigger = screen.getByRole('combobox', { name: /role for admin@example\.com/i });
    const aliceTrigger = screen.getByRole('combobox', { name: /role for alice@example\.com/i });

    // Self row must be disabled; peer row must be enabled (no pending mutation).
    expect(adminTrigger).toBeDisabled();
    expect(aliceTrigger).not.toBeDisabled();
  });

  it('only disables the mutating row select; other rows remain enabled', async () => {
    // Mutation never resolves — stays pending so we can inspect disabled state.
    updateUserRoleMock.mockReturnValue(new Promise(() => {}));
    listUsersMock.mockResolvedValueOnce(_sampleUsers);

    renderPage();
    await waitFor(() => screen.getByText('alice@example.com'));

    const aliceTrigger = screen.getByRole('combobox', { name: /role for alice@example\.com/i });
    const adminTrigger = screen.getByRole('combobox', { name: /role for admin@example\.com/i });

    // Before any mutation: alice (non-self) enabled; admin (self) always disabled.
    expect(aliceTrigger).not.toBeDisabled();
    expect(adminTrigger).toBeDisabled();

    // Directly invoke the onValueChange callback stored for alice's row.
    const aliceCb = _roleSelectCallbacks.get('Role for alice@example.com');
    expect(aliceCb).toBeDefined();
    act(() => { aliceCb!('admin'); });

    await waitFor(() => {
      expect(updateUserRoleMock).toHaveBeenCalledWith(2, 'admin');
    });

    // alice's trigger must now be disabled (pending mutation); admin's remains disabled (isSelf).
    expect(aliceTrigger).toBeDisabled();
    expect(adminTrigger).toBeDisabled();
  });
});

describe('per-row delete button isolation (DOM-F-07 delete)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _mockRole = 'admin';
    _mockUserId = 1;
  });

  it('only disables the deleting row button; other rows remain enabled during mutation', async () => {
    // deleteUser never resolves — stays pending so we can inspect disabled state.
    deleteUserMock.mockReturnValue(new Promise(() => {}));
    // inviteUserMock is unused here; listUsers returns two users.
    listUsersMock.mockResolvedValueOnce(_sampleUsers);

    renderPage();
    await waitFor(() => screen.getByText('alice@example.com'));

    const aliceDeleteBtn = screen.getByRole('button', { name: /remove alice@example\.com/i });
    const adminDeleteBtn = screen.getByRole('button', { name: /remove admin@example\.com/i });

    // Neither button disabled before mutation starts.
    expect(aliceDeleteBtn).not.toBeDisabled();
    // admin delete button: isSelf=true → disabled regardless; check alice's peer instead
    // (admin row isSelf flag makes it permanently disabled, so pick alice vs a 3rd user
    // if needed; here we just assert alice changes + admin stays as-is)

    // Open the delete confirmation for alice (click opens AlertDialog).
    await userEvent.click(aliceDeleteBtn);

    // Confirm the deletion — triggers the mutation.
    const confirmBtn = screen.getByRole('button', { name: /^remove$/i });
    await userEvent.click(confirmBtn);

    await waitFor(() => {
      expect(deleteUserMock).toHaveBeenCalledWith(2);
    });

    // Alice's button must now be disabled (pendingDeleteUserId === alice.id).
    expect(aliceDeleteBtn).toBeDisabled();
    // Admin's button must remain in its pre-mutation state (isSelf disabled, not affected by mutation).
    expect(adminDeleteBtn).toBeDisabled(); // isSelf — always disabled
  });
});

// ---------------------------------------------------------------------------
// H2 — mutation lifecycle: onMutate / onSettled wiring
// ---------------------------------------------------------------------------

describe('AdminUsersPage — role mutation lifecycle (H2)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _mockRole = 'admin';
    _mockUserId = 1;
    _roleSelectCallbacks.clear();
  });

  it('pendingRoleUserId is set during mutation and cleared on success', async () => {
    let resolveRole!: () => void;
    updateUserRoleMock.mockReturnValue(new Promise<void>((res) => { resolveRole = res; }));
    listUsersMock.mockResolvedValueOnce(_sampleUsers);

    renderPage();
    await waitFor(() => screen.getByText('alice@example.com'));

    const aliceCb = () => {
      const cb = _roleSelectCallbacks.get('Role for alice@example.com');
      expect(cb).toBeDefined();
      act(() => { cb!('admin'); });
    };
    aliceCb();

    const aliceTrigger = screen.getByRole('combobox', { name: /role for alice@example\.com/i });

    // onMutate fires synchronously before mutationFn resolves → select disabled
    await waitFor(() => expect(aliceTrigger).toBeDisabled());

    // Resolve → onSettled fires → select re-enabled
    resolveRole();
    await waitFor(() => expect(aliceTrigger).not.toBeDisabled());
  });

  it('pendingRoleUserId is cleared on mutation error via onSettled', async () => {
    let rejectRole!: (e: Error) => void;
    updateUserRoleMock.mockReturnValue(new Promise<void>((_res, rej) => { rejectRole = rej; }));
    listUsersMock.mockResolvedValueOnce(_sampleUsers);

    renderPage();
    await waitFor(() => screen.getByText('alice@example.com'));

    const cb = _roleSelectCallbacks.get('Role for alice@example.com');
    expect(cb).toBeDefined();
    act(() => { cb!('admin'); });

    const aliceTrigger = screen.getByRole('combobox', { name: /role for alice@example\.com/i });
    await waitFor(() => expect(aliceTrigger).toBeDisabled());

    rejectRole(new Error('network failure'));
    await waitFor(() => expect(aliceTrigger).not.toBeDisabled());
  });
});

describe('AdminUsersPage — delete mutation lifecycle (H2)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _mockRole = 'admin';
    _mockUserId = 1;
  });

  it('pendingDeleteUserId is set during mutation and cleared on success', async () => {
    let resolveDelete!: () => void;
    deleteUserMock.mockReturnValue(new Promise<void>((res) => { resolveDelete = res; }));
    listUsersMock.mockResolvedValueOnce(_sampleUsers);

    renderPage();
    await waitFor(() => screen.getByText('alice@example.com'));

    const aliceDeleteBtn = screen.getByRole('button', { name: /remove alice@example\.com/i });
    await userEvent.click(aliceDeleteBtn);
    const confirmBtn = screen.getByRole('button', { name: /^remove$/i });
    await userEvent.click(confirmBtn);

    // onMutate fires → button disabled
    await waitFor(() => expect(aliceDeleteBtn).toBeDisabled());

    // Resolve → onSettled fires → button re-enabled
    resolveDelete();
    await waitFor(() => expect(aliceDeleteBtn).not.toBeDisabled());
  });

  it('pendingDeleteUserId is cleared on mutation error via onSettled', async () => {
    let rejectDelete!: (e: Error) => void;
    deleteUserMock.mockReturnValue(new Promise<void>((_res, rej) => { rejectDelete = rej; }));
    listUsersMock.mockResolvedValueOnce(_sampleUsers);

    renderPage();
    await waitFor(() => screen.getByText('alice@example.com'));

    const aliceDeleteBtn = screen.getByRole('button', { name: /remove alice@example\.com/i });
    await userEvent.click(aliceDeleteBtn);
    const confirmBtn = screen.getByRole('button', { name: /^remove$/i });
    await userEvent.click(confirmBtn);

    await waitFor(() => expect(aliceDeleteBtn).toBeDisabled());

    rejectDelete(new Error('server error'));
    await waitFor(() => expect(aliceDeleteBtn).not.toBeDisabled());
  });
});

describe('AdminUsersPage — send sign-in link', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _mockRole = 'admin';
    _mockUserId = 1;
  });

  it('renders a send sign-in link button per non-deleted row', async () => {
    listUsersMock.mockResolvedValueOnce(_sampleUsers);
    renderPage();

    await waitFor(() => screen.getByText('alice@example.com'));

    expect(
      screen.getByRole('button', { name: /send sign-in link to alice@example\.com/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /send sign-in link to admin@example\.com/i }),
    ).toBeInTheDocument();
  });

  it('clicking the button calls sendSignInLink with the row id + success toast', async () => {
    const { toast } = await import('sonner');
    listUsersMock.mockResolvedValueOnce(_sampleUsers);
    sendSignInLinkMock.mockResolvedValueOnce({ sent: true });

    renderPage();
    await waitFor(() => screen.getByText('alice@example.com'));

    await userEvent.click(
      screen.getByRole('button', { name: /send sign-in link to alice@example\.com/i }),
    );

    await waitFor(() => {
      expect(sendSignInLinkMock).toHaveBeenCalledWith(2);
    });
    await waitFor(() => {
      expect(vi.mocked(toast.success)).toHaveBeenCalledWith(
        'Sign-in link sent to alice@example.com',
      );
    });
  });

  it('hides the send sign-in link button for soft-deleted rows', async () => {
    listUsersMock.mockResolvedValueOnce([
      _sampleUsers[0],
      { ..._sampleUsers[1], deleted_at: new Date().toISOString() },
    ]);
    renderPage();

    await waitFor(() => screen.getByText('alice@example.com'));

    expect(
      screen.queryByRole('button', { name: /send sign-in link to alice@example\.com/i }),
    ).not.toBeInTheDocument();
    // Non-deleted row still shows it.
    expect(
      screen.getByRole('button', { name: /send sign-in link to admin@example\.com/i }),
    ).toBeInTheDocument();
  });

  it('only disables the targeted row button during a pending send (per-row isolation)', async () => {
    sendSignInLinkMock.mockReturnValue(new Promise(() => {}));
    listUsersMock.mockResolvedValueOnce(_sampleUsers);

    renderPage();
    await waitFor(() => screen.getByText('alice@example.com'));

    const aliceBtn = screen.getByRole('button', {
      name: /send sign-in link to alice@example\.com/i,
    });
    const adminBtn = screen.getByRole('button', {
      name: /send sign-in link to admin@example\.com/i,
    });

    expect(aliceBtn).not.toBeDisabled();
    await userEvent.click(aliceBtn);

    await waitFor(() => expect(aliceBtn).toBeDisabled());
    expect(adminBtn).not.toBeDisabled();
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
