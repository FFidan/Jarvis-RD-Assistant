import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConfidenceBadge } from '@/components/chat/ConfidenceBadge';
import { ChatMessage } from '@/components/chat/ChatMessage';
import type { ChatMessage as ChatMessageType } from '@/types';

// ---------------------------------------------------------------------------
// ConfidenceBadge unit tests
// ---------------------------------------------------------------------------

describe('ConfidenceBadge', () => {
  it('renders "Verified" with green styling for HIGH confidence', () => {
    const perSentence = [
      { text: 'Sentence one.', verified: true },
      { text: 'Sentence two.', verified: true },
    ];
    render(
      <ConfidenceBadge
        confidence="HIGH"
        verified_fraction={1}
        per_sentence={perSentence}
      />,
    );
    const badge = screen.getByText('Verified');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/green/);
  });

  it('renders "Mostly verified" with yellow styling for MEDIUM confidence', () => {
    const perSentence = [
      { text: 'Sentence one.', verified: true },
      { text: 'Sentence two.', verified: false },
    ];
    render(
      <ConfidenceBadge
        confidence="MEDIUM"
        verified_fraction={0.5}
        per_sentence={perSentence}
      />,
    );
    const badge = screen.getByText('Mostly verified');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/yellow/);
  });

  it('tooltip text mentions N/M ratio for MEDIUM confidence', async () => {
    const user = userEvent.setup();
    const perSentence = [
      { text: 'First.', verified: true },
      { text: 'Second.', verified: false },
      { text: 'Third.', verified: false },
    ];
    render(
      <ConfidenceBadge
        confidence="MEDIUM"
        verified_fraction={1 / 3}
        per_sentence={perSentence}
      />,
    );
    // Hover to reveal tooltip (Radix renders text in visible + sr-only span)
    await user.hover(screen.getByText('Mostly verified'));
    const tooltipMatches = await screen.findAllByText(/1 of 3 sentences/);
    expect(tooltipMatches.length).toBeGreaterThan(0);
  });

  it('renders "Partially verified" with orange styling for LOW confidence', () => {
    render(
      <ConfidenceBadge
        confidence="LOW"
        verified_fraction={0.2}
        per_sentence={[{ text: 'Only sentence.', verified: false }]}
      />,
    );
    const badge = screen.getByText('Partially verified');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/orange/);
  });

  it('renders "Unverified" with red styling for UNVERIFIED confidence', () => {
    render(
      <ConfidenceBadge
        confidence="UNVERIFIED"
        verified_fraction={0}
        per_sentence={[{ text: 'No source.', verified: false }]}
      />,
    );
    const badge = screen.getByText('Unverified');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/red/);
  });
});

// ---------------------------------------------------------------------------
// ChatMessage integration tests
// ---------------------------------------------------------------------------

describe('ChatMessage confidence badge integration', () => {
  it('shows "Verified" badge when message has HIGH confidence', () => {
    const message: ChatMessageType = {
      role: 'assistant',
      content: 'The answer is 42.',
      confidence: 'HIGH',
      verified_fraction: 1,
      per_sentence: [{ text: 'The answer is 42.', verified: true }],
    };
    render(<ChatMessage message={message} />);
    expect(screen.getByText('Verified')).toBeInTheDocument();
  });

  it('shows "Mostly verified" badge when message has MEDIUM confidence', () => {
    const message: ChatMessageType = {
      role: 'assistant',
      content: 'Some content here.',
      confidence: 'MEDIUM',
      verified_fraction: 0.5,
      per_sentence: [
        { text: 'Some content here.', verified: true },
        { text: 'Extra sentence.', verified: false },
      ],
    };
    render(<ChatMessage message={message} />);
    expect(screen.getByText('Mostly verified')).toBeInTheDocument();
  });

  it('does NOT render badge when message has no confidence field', () => {
    const message: ChatMessageType = {
      role: 'assistant',
      content: 'A plain response.',
    };
    render(<ChatMessage message={message} />);
    expect(screen.queryByText('Verified')).not.toBeInTheDocument();
    expect(screen.queryByText('Mostly verified')).not.toBeInTheDocument();
    expect(screen.queryByText('Partially verified')).not.toBeInTheDocument();
    expect(screen.queryByText('Unverified')).not.toBeInTheDocument();
  });

  it('does NOT render badge for user messages even if confidence is set', () => {
    // ChatMessage only shows badge for assistant content block
    const message: ChatMessageType = {
      role: 'user',
      content: 'A user question.',
      // @ts-expect-error: confidence not expected on user messages but guard anyway
      confidence: 'HIGH',
    };
    render(<ChatMessage message={message} />);
    expect(screen.queryByText('Verified')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Dialog open test
// ---------------------------------------------------------------------------

describe('ConfidenceBadge dialog', () => {
  it('opens dialog with unverified sentences on badge click', async () => {
    const user = userEvent.setup();
    const perSentence = [
      { text: 'Confirmed by source A.', verified: true },
      { text: 'This claim is unverified.', verified: false },
    ];
    render(
      <ConfidenceBadge
        confidence="MEDIUM"
        verified_fraction={0.5}
        per_sentence={perSentence}
      />,
    );
    await user.click(screen.getByText('Mostly verified'));
    expect(await screen.findByText('Answer Verification Details')).toBeInTheDocument();
    expect(screen.getByText('This claim is unverified.')).toBeInTheDocument();
    // Verified sentences should NOT appear in the unverified list
    expect(screen.queryByText('Confirmed by source A.')).not.toBeInTheDocument();
  });
});
