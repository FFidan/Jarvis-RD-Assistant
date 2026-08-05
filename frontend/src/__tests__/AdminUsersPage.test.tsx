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
import { screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AdminUsersPage } from '@/pages/AdminUsersPage';
import { AdminOnlyRoute } from '@/components/auth/AdminOnlyRoute';
import { ApiError } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const listUsersMock = vi.fn();
const inviteUserMock = vi.fn();
const updateUserRoleMock = vi.fn();
const deleteUserMock = vi.fn();
const restoreUserMock = vi.fn();
const sendSignInLinkMock = vi.fn();
const transferOwnerMock = vi.fn();

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

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
    restoreUser: (userId: number) => restoreUserMock(userId),
    sendSignInLink: (userId: number) => sendSignInLinkMock(userId),
    transferOwner: (userId: number, confirmation: string) =>
      transferOwnerMock(userId, confirmation),
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
    is_owner: true,
    owner_source: 'database',
    owner_state: 'valid',
  },
  {
    id: 2,
    email: 'alice@example.com',
    role: 'user',
    created_at: new Date().toISOString(),
    last_login_at: null,
    is_owner: false,
    owner_source: 'database',
    owner_state: 'valid',
  },
];

function renderPage() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <MemoryRouter initialEntries={['/admin/users']}>
      <Routes>
        <Route path="/admin/users" element={<AdminUsersPage />} />
        <Route path="/" element={<div>HOME</div>} />
      </Routes>
    </MemoryRouter>,
    { queryClient },
  );
}

function renderWithGuard(role: 'user' | 'admin') {
  _mockRole = role;
  const queryClient = createTestQueryClient();
  return renderWithProviders(
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
    </MemoryRouter>,
    { queryClient },
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

  it('points a 409 invite failure at the per-row Send sign-in link rescue', async () => {
    listUsersMock.mockResolvedValue(_sampleUsers);
    inviteUserMock.mockRejectedValueOnce(
      new ApiError(409, '{"detail":"A user with that email already exists"}'),
    );

    renderPage();
    await waitFor(() => screen.getByText('admin@example.com'));

    await userEvent.click(screen.getByRole('button', { name: /invite user/i }));
    await userEvent.type(screen.getByLabelText(/email address/i), 'alice@example.com');
    await userEvent.click(screen.getByRole('button', { name: /send invite/i }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/send sign-in link/i);
    });
  });
});

describe('AdminUsersPage — instance owner lifecycle', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _mockRole = 'admin';
    _mockUserId = 1;
    _roleSelectCallbacks.clear();
  });

  it('marks the owner and disables demotion and removal even for another admin', async () => {
    _mockUserId = 2;
    listUsersMock.mockResolvedValueOnce(_sampleUsers);
    renderPage();

    await waitFor(() => screen.getByText('admin@example.com'));

    expect(screen.getByText('instance owner')).toBeInTheDocument();
    expect(
      screen.getByRole('combobox', { name: /role for admin@example\.com/i }),
    ).toBeDisabled();
    expect(
      screen.getByRole('button', { name: /remove admin@example\.com/i }),
    ).toBeDisabled();
  });

  it('shows host-only guidance when OWNER_USER_ID controls ownership', async () => {
    const environmentUsers = _sampleUsers.map((user) => ({
      ...user,
      owner_source: 'environment',
    }));
    listUsersMock.mockResolvedValueOnce(environmentUsers);
    renderPage();

    await waitFor(() => screen.getByText('admin@example.com'));

    expect(screen.getByText(/ownership is managed on the host with OWNER_USER_ID/i))
      .toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /transfer ownership to/i }))
      .not.toBeInTheDocument();
  });

  it('shows the host recovery command when ownership is missing or invalid', async () => {
    const invalidUsers = _sampleUsers.map((user) => ({
      ...user,
      is_owner: false,
      owner_source: 'database',
      owner_state: 'missing',
    }));
    listUsersMock.mockResolvedValueOnce(invalidUsers);
    renderPage();

    await waitFor(() => screen.getByText('admin@example.com'));

    expect(screen.getByText(/jarvis-research owner status/i)).toBeInTheDocument();
    expect(screen.getByText(/jarvis-research owner set/i)).toBeInTheDocument();
  });

  it('requires the target email before transferring to another administrator', async () => {
    const targetEmail = 'alice@example.com';
    listUsersMock.mockResolvedValueOnce([
      _sampleUsers[0],
      { ..._sampleUsers[1], role: 'admin' },
    ]);
    transferOwnerMock.mockResolvedValueOnce({
      source: 'database',
      state: 'valid',
      user_id: 2,
    });
    renderPage();

    await waitFor(() => screen.getByText(targetEmail));
    await userEvent.click(
      screen.getByRole('button', { name: /transfer ownership to alice@example\.com/i }),
    );

    const confirmation = screen.getByLabelText(/type alice@example\.com to confirm/i);
    const submit = screen.getByRole('button', { name: /^transfer ownership$/i });
    expect(submit).toBeDisabled();

    await userEvent.type(confirmation, targetEmail);
    expect(submit).not.toBeDisabled();
    await userEvent.click(submit);

    await waitFor(() => {
      expect(transferOwnerMock).toHaveBeenCalledWith(2, targetEmail);
    });
  });

  it('does not offer transfer controls to an administrator who is not the owner', async () => {
    _mockUserId = 2;
    listUsersMock.mockResolvedValueOnce([
      _sampleUsers[0],
      { ..._sampleUsers[1], role: 'admin' },
    ]);
    renderPage();

    await waitFor(() => screen.getByText('alice@example.com'));

    expect(screen.queryByRole('button', { name: /transfer ownership to/i }))
      .not.toBeInTheDocument();
  });
});

describe('per-row role select isolation', () => {
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

describe('per-row delete button isolation', () => {
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
// mutation lifecycle: onMutate / onSettled wiring
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
    // Persistent, not Once: the success path invalidates and refetches the
    // users query, so the second fetch needs data too.
    listUsersMock.mockResolvedValue(_sampleUsers);

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
    // Persistent, not Once: the success path invalidates and refetches the
    // users query, so the second fetch needs data too.
    listUsersMock.mockResolvedValue(_sampleUsers);

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

  it('surfaces the manual link to copy when SMTP cannot deliver it', async () => {
    const { toast } = await import('sonner');
    const link = 'https://localhost:3001/auth/verify?token=xyz789';
    listUsersMock.mockResolvedValueOnce(_sampleUsers);
    sendSignInLinkMock.mockResolvedValueOnce({ sent: true, sent_link: link });

    renderPageWithCache(true);
    await waitFor(() => screen.getByText('alice@example.com'));

    await userEvent.click(
      screen.getByRole('button', { name: /send sign-in link to alice@example\.com/i }),
    );

    await waitFor(() => {
      expect(screen.getByLabelText(/sign-in link to share/i)).toHaveValue(link);
    });
    expect(screen.getByRole('status')).toHaveTextContent(/could not deliver/i);
    expect(screen.getByRole('status')).not.toHaveTextContent(/smtp is not configured/i);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    // The link goes to the dialog, not a transient toast.
    expect(vi.mocked(toast.success)).not.toHaveBeenCalled();
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

// ---------------------------------------------------------------------------
// Restore a soft-deleted user from the users table
// ---------------------------------------------------------------------------

describe('AdminUsersPage — restore soft-deleted user', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _mockRole = 'admin';
    _mockUserId = 1;
  });

  function renderPageWithSpiedClient() {
    const queryClient = createTestQueryClient();
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');
    renderWithProviders(
      <MemoryRouter initialEntries={['/admin/users']}>
        <Routes>
          <Route path="/admin/users" element={<AdminUsersPage />} />
          <Route path="/" element={<div>HOME</div>} />
        </Routes>
      </MemoryRouter>,
      { queryClient },
    );
    return { invalidateSpy };
  }

  const deletedAlice = { ..._sampleUsers[1], deleted_at: new Date().toISOString() };

  it('renders a Restore control (not Send/Trash) for a soft-deleted row, restores on click, and invalidates the users query', async () => {
    listUsersMock.mockResolvedValue([_sampleUsers[0], deletedAlice]);
    restoreUserMock.mockResolvedValueOnce({ ..._sampleUsers[1], deleted_at: null });

    const { invalidateSpy } = renderPageWithSpiedClient();
    await waitFor(() => screen.getByText('alice@example.com'));

    const restoreBtn = screen.getByRole('button', { name: /restore alice@example\.com/i });
    expect(restoreBtn).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /send sign-in link to alice@example\.com/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /remove alice@example\.com/i }),
    ).not.toBeInTheDocument();

    await userEvent.click(restoreBtn);

    await waitFor(() => {
      expect(restoreUserMock).toHaveBeenCalledWith(2);
    });
    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QUERY_KEYS.admin.users() });
    });
  });

  it('surfaces the backend error detail via toast when restore fails (409 model-hmac)', async () => {
    const { toast } = await import('sonner');
    listUsersMock.mockResolvedValue([_sampleUsers[0], deletedAlice]);
    restoreUserMock.mockRejectedValueOnce(
      new ApiError(
        409,
        '{"detail":"Set JARVIS_MODEL_HMAC_KEY (>=32 chars) before adding or restoring additional users — a derived key is unsafe on a multi-user deployment."}',
      ),
    );

    renderPage();
    await waitFor(() => screen.getByText('alice@example.com'));

    await userEvent.click(screen.getByRole('button', { name: /restore alice@example\.com/i }));

    await waitFor(() => {
      expect(restoreUserMock).toHaveBeenCalledWith(2);
    });
    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
        expect.stringContaining('JARVIS_MODEL_HMAC_KEY'),
      );
    });
  });
});

// ---------------------------------------------------------------------------
// B2/OPS-2 — invite deliverability: manual-link fallback when SMTP is off
// ---------------------------------------------------------------------------

function renderPageWithCache(smtpConfigured: boolean | undefined) {
  const queryClient = createTestQueryClient();
  if (smtpConfigured !== undefined) {
    queryClient.setQueryData(QUERY_KEYS.setup.firstRun(), {
      smtp_configured: smtpConfigured,
    });
  }
  return renderWithProviders(
    <MemoryRouter initialEntries={['/admin/users']}>
      <Routes>
        <Route path="/admin/users" element={<AdminUsersPage />} />
        <Route path="/" element={<div>HOME</div>} />
      </Routes>
    </MemoryRouter>,
    { queryClient },
  );
}

describe('AdminUsersPage — invite deliverability (B2/OPS-2)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _mockRole = 'admin';
    _mockUserId = 1;
  });

  it('shows the manual-share notice and the link when SMTP is unconfigured', async () => {
    const link = 'https://localhost:3001/auth/verify?token=abc123';
    listUsersMock.mockResolvedValue(_sampleUsers);
    inviteUserMock.mockResolvedValueOnce({
      id: 3,
      email: 'new@example.com',
      role: 'user',
      created_at: new Date().toISOString(),
      last_login_at: null,
      invite_link: link,
    });

    renderPageWithCache(false);
    await waitFor(() => screen.getByText('admin@example.com'));

    await userEvent.click(screen.getByRole('button', { name: /invite user/i }));
    await userEvent.type(screen.getByLabelText(/email address/i), 'new@example.com');
    await userEvent.click(screen.getByRole('button', { name: /send invite/i }));

    // Modal stays open, shows the SMTP notice + the link to copy.
    await waitFor(() => {
      expect(screen.getByText(/automatic email is not configured/i)).toBeInTheDocument();
    });
    expect(screen.getByLabelText(/invite sign-in link/i)).toHaveValue(link);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('does not blame missing SMTP when a configured relay fails', async () => {
    const link = 'https://localhost:3001/auth/verify?token=relay-failed';
    listUsersMock.mockResolvedValue(_sampleUsers);
    inviteUserMock.mockResolvedValueOnce({
      id: 3,
      email: 'new@example.com',
      role: 'user',
      created_at: new Date().toISOString(),
      last_login_at: null,
      invite_link: link,
    });

    renderPageWithCache(true);
    await waitFor(() => screen.getByText('admin@example.com'));
    await userEvent.click(screen.getByRole('button', { name: /invite user/i }));
    await userEvent.type(screen.getByLabelText(/email address/i), 'new@example.com');
    await userEvent.click(screen.getByRole('button', { name: /send invite/i }));

    const notice = await screen.findByRole('status');
    expect(notice).toHaveTextContent(/could not deliver/i);
    expect(notice).not.toHaveTextContent(/smtp is not configured/i);
    expect(screen.getByLabelText(/invite sign-in link/i)).toHaveValue(link);
  });

  it('closes the modal with no link shown when SMTP is configured', async () => {
    listUsersMock.mockResolvedValue(_sampleUsers);
    inviteUserMock.mockResolvedValueOnce({
      id: 3,
      email: 'new@example.com',
      role: 'user',
      created_at: new Date().toISOString(),
      last_login_at: null,
      // No invite_link: backend omits it when delivery succeeded.
    });

    renderPageWithCache(true);
    await waitFor(() => screen.getByText('admin@example.com'));

    await userEvent.click(screen.getByRole('button', { name: /invite user/i }));
    await userEvent.type(screen.getByLabelText(/email address/i), 'new@example.com');
    await userEvent.click(screen.getByRole('button', { name: /send invite/i }));

    await waitFor(() => {
      expect(inviteUserMock).toHaveBeenCalledWith('new@example.com', 'user');
    });
    // Modal closes; no link surfaced.
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(screen.queryByLabelText(/invite sign-in link/i)).not.toBeInTheDocument();
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

// ---------------------------------------------------------------------------
// H5g — deleteMutation onError fires toast
// ---------------------------------------------------------------------------

describe('AdminUsersPage — delete mutation onError toast (H5g)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _mockRole = 'admin';
    _mockUserId = 1;
  });

  it('fires toast.error when deleteUser fails', async () => {
    const { toast } = await import('sonner');
    deleteUserMock.mockRejectedValueOnce(new Error('server error'));
    listUsersMock.mockResolvedValueOnce(_sampleUsers);

    renderPage();
    await waitFor(() => screen.getByText('alice@example.com'));

    // Open delete confirmation for alice
    await userEvent.click(screen.getByRole('button', { name: /remove alice@example\.com/i }));
    // Confirm in the AlertDialog
    await userEvent.click(screen.getByRole('button', { name: /^remove$/i }));

    await waitFor(() => {
      expect(deleteUserMock).toHaveBeenCalledWith(2);
    });
    await waitFor(() => {
      expect(vi.mocked(toast.error)).toHaveBeenCalled();
    });
  });
});
