/**
 * Unit tests for AccountSection.
 *
 * Coverage:
 *  - Profile render (display_name, email, role, dates)
 *  - display_name edit (save + cancel)
 *  - Email-change flow (request → sent banner; cancel path)
 *  - ?confirm_email_token query-param → confirmEmailChange called → success banner + param stripped
 *  - Error states for both mutations
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { userEvent } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AccountSection } from '@/components/settings/AccountSection';

// ---------------------------------------------------------------------------
// API mock
// ---------------------------------------------------------------------------

const mockFetchAccount = vi.fn();
const mockUpdateAccount = vi.fn();
const mockConfirmEmailChange = vi.fn();
const mockDownloadMyData = vi.fn();

vi.mock('@/lib/api', () => ({
  fetchAccount: (...args: unknown[]) => mockFetchAccount(...args),
  updateAccount: (...args: unknown[]) => mockUpdateAccount(...args),
  confirmEmailChange: (...args: unknown[]) => mockConfirmEmailChange(...args),
  downloadMyData: (...args: unknown[]) => mockDownloadMyData(...args),
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const ACCOUNT = {
  id: 1,
  email: 'user@example.com',
  role: 'admin',
  display_name: 'Ada Lovelace',
  created_at: '2025-01-15T10:00:00Z',
  last_login_at: '2026-05-15T08:30:00Z',
};

// ---------------------------------------------------------------------------
// Render helper
// ---------------------------------------------------------------------------

function renderAccountSection(initialSearch = '') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/settings${initialSearch}`]}>
        <AccountSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AccountSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchAccount.mockResolvedValue(ACCOUNT);
    mockDownloadMyData.mockResolvedValue(undefined);
  });

  // --- Profile render ---

  it('renders loading state initially', () => {
    // fetchAccount never resolves during this render
    mockFetchAccount.mockImplementation(() => new Promise(() => {}));
    renderAccountSection();
    expect(screen.getByText(/Loading profile/i)).toBeInTheDocument();
  });

  it('renders profile data after load', async () => {
    renderAccountSection();
    await waitFor(() => expect(screen.getByTestId('display-name-value')).toBeInTheDocument());
    expect(screen.getByTestId('display-name-value')).toHaveTextContent('Ada Lovelace');
    expect(screen.getByTestId('email-value')).toHaveTextContent('user@example.com');
    expect(screen.getByTestId('role-value')).toHaveTextContent('admin');
  });

  it('shows error state when fetchAccount rejects', async () => {
    mockFetchAccount.mockRejectedValue(new Error('Network error'));
    renderAccountSection();
    await waitFor(() =>
      expect(screen.getByText(/Failed to load account profile/i)).toBeInTheDocument(),
    );
  });

  it('shows "Not set" when display_name is null', async () => {
    mockFetchAccount.mockResolvedValue({ ...ACCOUNT, display_name: null });
    renderAccountSection();
    await waitFor(() => expect(screen.getByTestId('display-name-value')).toBeInTheDocument());
    expect(screen.getByTestId('display-name-value')).toHaveTextContent(/not set/i);
  });

  // --- display_name edit ---

  it('enters display_name edit mode on pencil click', async () => {
    const user = userEvent.setup();
    renderAccountSection();
    await waitFor(() => screen.getByTestId('display-name-value'));

    await user.click(screen.getByRole('button', { name: /edit display name/i }));
    const input = screen.getByRole('textbox', { name: /display name/i });
    expect(input).toBeInTheDocument();
    expect(input).toHaveValue('Ada Lovelace');
  });

  it('saves display_name on checkmark click', async () => {
    const user = userEvent.setup();
    mockUpdateAccount.mockResolvedValue({
      account: { ...ACCOUNT, display_name: 'Grace Hopper' },
      email_verification_sent: false,
    });
    renderAccountSection();
    await waitFor(() => screen.getByTestId('display-name-value'));

    await user.click(screen.getByRole('button', { name: /edit display name/i }));
    const input = screen.getByRole('textbox', { name: /display name/i });
    await user.clear(input);
    await user.type(input, 'Grace Hopper');
    await user.click(screen.getByRole('button', { name: /save display name/i }));

    await waitFor(() => expect(mockUpdateAccount).toHaveBeenCalledWith({ display_name: 'Grace Hopper' }));
  });

  it('cancels display_name edit without calling updateAccount', async () => {
    const user = userEvent.setup();
    renderAccountSection();
    await waitFor(() => screen.getByTestId('display-name-value'));

    await user.click(screen.getByRole('button', { name: /edit display name/i }));
    await user.click(screen.getByRole('button', { name: /cancel display name edit/i }));
    expect(mockUpdateAccount).not.toHaveBeenCalled();
    expect(screen.getByTestId('display-name-value')).toBeInTheDocument();
  });

  it('shows error when updateAccount rejects (display_name)', async () => {
    const user = userEvent.setup();
    mockUpdateAccount.mockRejectedValue(new Error('Server error'));
    renderAccountSection();
    await waitFor(() => screen.getByTestId('display-name-value'));

    await user.click(screen.getByRole('button', { name: /edit display name/i }));
    await user.click(screen.getByRole('button', { name: /save display name/i }));

    await waitFor(() => expect(screen.getByText(/Server error/i)).toBeInTheDocument());
  });

  // --- Email-change flow ---

  it('shows email-change input on pencil click', async () => {
    const user = userEvent.setup();
    renderAccountSection();
    await waitFor(() => screen.getByTestId('email-value'));

    await user.click(screen.getByRole('button', { name: /change email/i }));
    expect(screen.getByRole('textbox', { name: /new email address/i })).toBeInTheDocument();
  });

  it('calls updateAccount with new email and shows sent banner', async () => {
    const user = userEvent.setup();
    mockUpdateAccount.mockResolvedValue({
      account: ACCOUNT,
      email_verification_sent: true,
    });
    renderAccountSection();
    await waitFor(() => screen.getByTestId('email-value'));

    await user.click(screen.getByRole('button', { name: /change email/i }));
    const input = screen.getByRole('textbox', { name: /new email address/i });
    await user.clear(input);
    await user.type(input, 'new@example.com');
    await user.click(screen.getByRole('button', { name: /send verification/i }));

    await waitFor(() =>
      expect(screen.getByText(/Verification link sent/i)).toBeInTheDocument(),
    );
    expect(mockUpdateAccount).toHaveBeenCalledWith({ email: 'new@example.com' });
  });

  it('cancels email edit without calling updateAccount', async () => {
    const user = userEvent.setup();
    renderAccountSection();
    await waitFor(() => screen.getByTestId('email-value'));

    await user.click(screen.getByRole('button', { name: /change email/i }));
    await user.click(screen.getByRole('button', { name: /cancel/i }));
    expect(mockUpdateAccount).not.toHaveBeenCalled();
    expect(screen.getByTestId('email-value')).toBeInTheDocument();
  });

  it('shows error when updateAccount rejects (email)', async () => {
    const user = userEvent.setup();
    mockUpdateAccount.mockRejectedValue(new Error('Email already in use'));
    renderAccountSection();
    await waitFor(() => screen.getByTestId('email-value'));

    await user.click(screen.getByRole('button', { name: /change email/i }));
    const input = screen.getByRole('textbox', { name: /new email address/i });
    await user.clear(input);
    await user.type(input, 'taken@example.com');
    await user.click(screen.getByRole('button', { name: /send verification/i }));

    await waitFor(() => expect(screen.getByText(/Email already in use/i)).toBeInTheDocument());
  });

  // --- ?confirm_email_token query-param flow ---

  it('calls confirmEmailChange when ?confirm_email_token is present', async () => {
    mockConfirmEmailChange.mockResolvedValue({ ...ACCOUNT, email: 'confirmed@example.com' });
    renderAccountSection('?confirm_email_token=test-tok-123');

    await waitFor(() => expect(mockConfirmEmailChange).toHaveBeenCalledWith('test-tok-123'));
  });

  it('shows success banner after successful email confirmation', async () => {
    mockConfirmEmailChange.mockResolvedValue({ ...ACCOUNT, email: 'confirmed@example.com' });
    renderAccountSection('?confirm_email_token=test-tok-123');

    await waitFor(() =>
      expect(screen.getByText(/Email address updated to/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/confirmed@example.com/i)).toBeInTheDocument();
  });

  it('shows error banner when confirmEmailChange rejects', async () => {
    mockConfirmEmailChange.mockRejectedValue(new Error('Token expired'));
    renderAccountSection('?confirm_email_token=bad-tok');

    await waitFor(() =>
      expect(screen.getByText(/Token expired/i)).toBeInTheDocument(),
    );
  });

  it('does NOT call confirmEmailChange when no query param', async () => {
    renderAccountSection();
    await waitFor(() => screen.getByTestId('display-name-value'));
    expect(mockConfirmEmailChange).not.toHaveBeenCalled();
  });

  it('downloads the authenticated user account data export', async () => {
    const user = userEvent.setup();
    renderAccountSection();
    await waitFor(() => screen.getByTestId('display-name-value'));

    await user.click(screen.getByRole('button', { name: /download my data/i }));

    await waitFor(() => expect(mockDownloadMyData).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/Download started/i)).toBeInTheDocument();
  });

  it('shows an error when account data export fails', async () => {
    const user = userEvent.setup();
    mockDownloadMyData.mockRejectedValue(new Error('Export unavailable'));
    renderAccountSection();
    await waitFor(() => screen.getByTestId('display-name-value'));

    await user.click(screen.getByRole('button', { name: /download my data/i }));

    expect(await screen.findByText(/Account data export could not be downloaded/i)).toBeInTheDocument();
    expect(screen.queryByText(/Export unavailable/i)).not.toBeInTheDocument();
  });
});
