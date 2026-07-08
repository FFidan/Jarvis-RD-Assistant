import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { InfoTooltip } from '@/components/ui/info-tooltip';

describe('InfoTooltip', () => {
  it('renders info icon with aria-label', () => {
    render(<InfoTooltip content="hello world" />);
    const trigger = screen.getByLabelText('More info');
    expect(trigger).toBeInTheDocument();
  });

  it('shows tooltip content on hover', async () => {
    const user = userEvent.setup();
    render(<InfoTooltip content="tooltip body text" />);
    const trigger = screen.getByLabelText('More info');
    await user.hover(trigger);
    await waitFor(() => {
      // Radix renders content in a portal — there may be multiple copies
      // (visually-hidden a11y copy + the visible one). Use getAllByText.
      const matches = screen.getAllByText('tooltip body text');
      expect(matches.length).toBeGreaterThan(0);
    });
  });

  it('keeps span triggers keyboard focusable', async () => {
    const user = userEvent.setup();
    render(<InfoTooltip content="span tooltip text" triggerElement="span" />);
    const trigger = screen.getByRole('button', { name: 'More info' });

    await user.tab();
    expect(trigger).toHaveFocus();

    await waitFor(() => {
      expect(screen.getAllByText('span tooltip text').length).toBeGreaterThan(0);
    });
  });
});
