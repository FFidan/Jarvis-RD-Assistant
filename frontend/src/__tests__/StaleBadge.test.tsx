import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StaleBadge } from '@/components/pulse/StaleBadge';

describe('StaleBadge', () => {
  it('renders "yesterday" text when ageDays is 1', () => {
    render(<StaleBadge ageDays={1} diagnostics={null} />);
    expect(screen.getByTestId('stale-badge')).toHaveTextContent("Showing yesterday's deck");
  });

  it('renders N-days-ago text when ageDays > 1', () => {
    render(<StaleBadge ageDays={3} diagnostics={null} />);
    expect(screen.getByTestId('stale-badge')).toHaveTextContent('Showing deck from 3 days ago');
  });

  it('opens a sheet drawer when badge is clicked', async () => {
    const user = userEvent.setup();
    render(<StaleBadge ageDays={2} diagnostics={null} />);
    await user.click(screen.getByTestId('stale-badge'));
    await waitFor(() => {
      expect(screen.getByText('Outdated recommendations')).toBeInTheDocument();
    });
  });

  it('renders per-source diagnostics inside the sheet', async () => {
    const user = userEvent.setup();
    const diagnostics = {
      arxiv: {
        last_status: 'rate_limit',
        cooldown_until: '2026-08-09T18:00:00Z',
        consecutive_failures: 2,
      },
      openalex: {
        last_status: 'ok',
        cooldown_until: null,
        consecutive_failures: 0,
      },
    };
    render(<StaleBadge ageDays={1} diagnostics={diagnostics} />);
    await user.click(screen.getByTestId('stale-badge'));
    await waitFor(() => {
      expect(screen.getByText('arxiv')).toBeInTheDocument();
      expect(screen.getByText(/Paused until/)).toBeInTheDocument();
      expect(screen.getByText('openalex')).toBeInTheDocument();
      expect(screen.getByText('0 consecutive failures')).toBeInTheDocument();
    });
  });

  it('renders the Retry button when onRetry prop is provided', async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    render(<StaleBadge ageDays={1} diagnostics={null} onRetry={onRetry} />);
    await user.click(screen.getByTestId('stale-badge'));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /generate now/i })).toBeInTheDocument();
    });
  });

  it('calls onRetry and closes the sheet when Generate now is clicked', async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    render(<StaleBadge ageDays={2} diagnostics={null} onRetry={onRetry} />);
    await user.click(screen.getByTestId('stale-badge'));
    const btn = await screen.findByRole('button', { name: /generate now/i });
    await user.click(btn);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('does not render Retry button when onRetry is not provided', async () => {
    const user = userEvent.setup();
    render(<StaleBadge ageDays={1} diagnostics={null} />);
    await user.click(screen.getByTestId('stale-badge'));
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /generate now/i })).not.toBeInTheDocument();
    });
  });
});
