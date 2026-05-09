import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { VerificationBadge } from '@/components/shared/VerificationBadge';

describe('VerificationBadge', () => {
  describe('verified variant', () => {
    it('renders "Verified" label', () => {
      render(<VerificationBadge variant="verified" />);
      expect(screen.getByText('Verified')).toBeInTheDocument();
    });

    it('renders the check icon', () => {
      render(<VerificationBadge variant="verified" />);
      expect(screen.getByTestId('verification-badge-check-icon')).toBeInTheDocument();
    });

    it('does not render the warning icon', () => {
      render(<VerificationBadge variant="verified" />);
      expect(screen.queryByTestId('verification-badge-warn-icon')).not.toBeInTheDocument();
    });

    it('has aria-label "Reasoning verified"', () => {
      render(<VerificationBadge variant="verified" />);
      expect(screen.getByLabelText('Reasoning verified')).toBeInTheDocument();
    });

    it('shows tooltip with reason on hover when reason is provided', async () => {
      const user = userEvent.setup();
      render(<VerificationBadge variant="verified" reason="Verified at HIGH confidence" />);
      await user.hover(screen.getByLabelText('Reasoning verified'));
      await waitFor(() => {
        // Radix renders the tooltip text in both the visible tooltip div and a
        // hidden role="tooltip" span for accessibility; use role query to find
        // the accessible tooltip element specifically.
        expect(screen.getByRole('tooltip')).toHaveTextContent('Verified at HIGH confidence');
      });
    });

    it('does not render a Tooltip wrapper when no reason is provided', () => {
      render(<VerificationBadge variant="verified" />);
      // Without TooltipProvider, data-state attribute is absent
      const badge = screen.getByLabelText('Reasoning verified');
      expect(badge).not.toHaveAttribute('data-state');
    });
  });

  describe('unverified variant', () => {
    it('renders "Unverified" label', () => {
      render(<VerificationBadge variant="unverified" />);
      expect(screen.getByText('Unverified')).toBeInTheDocument();
    });

    it('renders the warning icon', () => {
      render(<VerificationBadge variant="unverified" />);
      expect(screen.getByTestId('verification-badge-warn-icon')).toBeInTheDocument();
    });

    it('does not render the check icon', () => {
      render(<VerificationBadge variant="unverified" />);
      expect(screen.queryByTestId('verification-badge-check-icon')).not.toBeInTheDocument();
    });

    it('has aria-label "Reasoning not verified"', () => {
      render(<VerificationBadge variant="unverified" />);
      expect(screen.getByLabelText('Reasoning not verified')).toBeInTheDocument();
    });

    it('shows tooltip with reason on hover when reason is provided', async () => {
      const user = userEvent.setup();
      const reason = 'Quote not found in paper abstract';
      render(<VerificationBadge variant="unverified" reason={reason} />);
      await user.hover(screen.getByLabelText('Reasoning not verified'));
      await waitFor(() => {
        // Radix renders the tooltip text in both the visible tooltip div and a
        // hidden role="tooltip" span for accessibility; use role query to find
        // the accessible tooltip element specifically.
        expect(screen.getByRole('tooltip')).toHaveTextContent(reason);
      });
    });

    it('renders without tooltip when no reason is provided', () => {
      render(<VerificationBadge variant="unverified" />);
      expect(screen.getByText('Unverified')).toBeInTheDocument();
    });
  });

  describe('PulseCard integration (smoke)', () => {
    it('verified variant does not render "Unverified" text', () => {
      render(<VerificationBadge variant="verified" reason="HIGH confidence" />);
      expect(screen.queryByText('Unverified')).not.toBeInTheDocument();
    });

    it('unverified variant does not render "Verified" text', () => {
      render(<VerificationBadge variant="unverified" reason="LOW confidence" />);
      expect(screen.queryByText(/^Verified$/)).not.toBeInTheDocument();
    });
  });
});
