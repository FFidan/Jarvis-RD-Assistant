import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConfidenceBadge } from '@/components/chat/ConfidenceBadge';
import { ChatMessage } from '@/components/chat/ChatMessage';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
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
      confidence: 'HIGH',
    };
    render(<ChatMessage message={message} />);
    expect(screen.queryByText('Verified')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// A2.2 — inline <mark> sentence highlighting tests
// ---------------------------------------------------------------------------

describe('ChatMessage inline sentence highlighting', () => {
  it('test_chat_message_highlights_unverified_sentences: <mark> wraps unverified sentence only', () => {
    const message: ChatMessageType = {
      role: 'assistant',
      content: 'First. Second.',
      confidence: 'MEDIUM',
      verified_fraction: 0.5,
      per_sentence: [
        { text: 'First.', verified: true },
        { text: 'Second.', verified: false },
      ],
    };
    const { container } = render(<ChatMessage message={message} />);
    const marks = container.querySelectorAll('mark');
    expect(marks).toHaveLength(1);
    expect(marks[0].textContent).toBe('Second.');
    // "First." must NOT be inside any <mark>
    const allMarkedText = Array.from(marks).map((m) => m.textContent).join('');
    expect(allMarkedText).not.toContain('First.');
  });

  it('test_chat_message_no_mark_when_all_verified: no <mark> elements when every sentence is verified', () => {
    const message: ChatMessageType = {
      role: 'assistant',
      content: 'Everything checks out.',
      confidence: 'HIGH',
      verified_fraction: 1,
      per_sentence: [{ text: 'Everything checks out.', verified: true }],
    };
    const { container } = render(<ChatMessage message={message} />);
    expect(container.querySelectorAll('mark')).toHaveLength(0);
  });

  it('test_chat_message_no_mark_when_no_per_sentence: no <mark> elements when per_sentence is absent', () => {
    const message: ChatMessageType = {
      role: 'assistant',
      content: 'A plain response with no verification data.',
    };
    const { container } = render(<ChatMessage message={message} />);
    expect(container.querySelectorAll('mark')).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// C.6 — MarkdownContent javascript: / data: href blocking tests
// ---------------------------------------------------------------------------

describe('MarkdownContent href blocking', () => {
  it('test_javascript_href_renders_as_text: no javascript: href reaches the DOM', () => {
    const { container } = render(
      <MarkdownContent>{'[click](javascript:alert(1))'}</MarkdownContent>,
    );
    // react-markdown sanitizes javascript: hrefs to "" before the components.a handler;
    // additionally our handler would block it. Either way, no javascript: href in DOM.
    const anchors = container.querySelectorAll('a');
    for (const a of anchors) {
      expect(a.getAttribute('href') ?? '').not.toMatch(/^javascript:/i);
    }
    // The link text "click" should still be present somewhere in the document
    expect(screen.getByText('click')).toBeInTheDocument();
  });

  it('test_data_url_blocked_except_image: no non-image data: href reaches the DOM', () => {
    const { container: blockedContainer } = render(
      <MarkdownContent>{'[click](data:text/html,<h1>XSS</h1>)'}</MarkdownContent>,
    );
    // react-markdown sanitizes data: hrefs to "" — our handler also blocks them
    const blockedAnchors = blockedContainer.querySelectorAll('a');
    for (const a of blockedAnchors) {
      expect(a.getAttribute('href') ?? '').not.toMatch(/^data:(?!image\/)/i);
    }
    // The text "click" is still rendered
    expect(screen.getByText('click')).toBeInTheDocument();

    // data:image/ in an <img> tag renders as an image element (not a link)
    const { container: imageContainer } = render(
      <MarkdownContent>{'![alt](data:image/png;base64,abc123)'}</MarkdownContent>,
    );
    // The img element is present (src may be sanitized to "" by jsdom but the tag exists)
    const img = imageContainer.querySelector('img');
    expect(img).toBeInTheDocument();
    expect(img?.getAttribute('alt')).toBe('alt');
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
