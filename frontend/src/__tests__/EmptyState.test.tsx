import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { AlertCircle } from 'lucide-react';
import { EmptyState } from '@/components/EmptyState';

function renderComponent(props: Partial<React.ComponentProps<typeof EmptyState>> = {}) {
  return render(
    <MemoryRouter>
      <EmptyState {...props} />
    </MemoryRouter>,
  );
}

describe('EmptyState', () => {
  it('renders default "No data" title and "Nothing to display yet." description', () => {
    renderComponent();
    expect(screen.getByText('No data')).toBeInTheDocument();
    expect(screen.getByText('Nothing to display yet.')).toBeInTheDocument();
  });

  it('renders custom title, description, and icon', () => {
    renderComponent({
      title: 'No results',
      description: 'Try adjusting your search.',
      icon: AlertCircle,
    });
    expect(screen.getByText('No results')).toBeInTheDocument();
    expect(screen.getByText('Try adjusting your search.')).toBeInTheDocument();
    // The default title/description should not appear
    expect(screen.queryByText('No data')).not.toBeInTheDocument();
    expect(screen.queryByText('Nothing to display yet.')).not.toBeInTheDocument();
  });

  it('renders Link button when actionLabel + actionHref provided', () => {
    renderComponent({
      actionLabel: 'Go to Feed',
      actionHref: '/feed',
    });
    const link = screen.getByRole('link', { name: 'Go to Feed' });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute('href', '/feed');
  });

  it('renders callback button when actionLabel + onAction (no actionHref)', async () => {
    const onAction = vi.fn();
    const user = userEvent.setup();
    renderComponent({
      actionLabel: 'Retry',
      onAction,
    });
    const button = screen.getByRole('button', { name: 'Retry' });
    expect(button).toBeInTheDocument();
    await user.click(button);
    expect(onAction).toHaveBeenCalledTimes(1);
  });

  it('renders no button when only actionLabel provided (no actionHref and no onAction)', () => {
    renderComponent({
      actionLabel: 'Orphan label',
    });
    expect(screen.queryByRole('button', { name: 'Orphan label' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Orphan label' })).not.toBeInTheDocument();
  });
});
