/**
 * FE-SSE-1 — StreamingChat renders role="alert" banner when streamError is set.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

// Mock the hook before importing the component
const mockUseStreamingChat = vi.fn();

vi.mock('@/hooks/use-streaming-chat', () => ({
  useStreamingChat: (...args: unknown[]) => mockUseStreamingChat(...args),
}));

// Minimal stubs for UI sub-components used inside StreamingChat
vi.mock('@/components/chat/ChatMessage', () => ({
  ChatMessage: ({
    isLoading,
    elapsedSeconds,
    isFirstQuestion,
  }: {
    isLoading?: boolean;
    elapsedSeconds?: number;
    isFirstQuestion?: boolean;
  }) =>
    isLoading ? (
      <div
        data-testid="chat-message-loading"
        data-elapsed={String(elapsedSeconds)}
        data-first={String(isFirstQuestion)}
      />
    ) : null,
}));
vi.mock('@/components/chat/SourcesAccordion', () => ({
  SourcesAccordion: () => null,
}));
vi.mock('@/components/ui/scroll-area', () => ({
  ScrollArea: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock('@/components/ui/button', () => ({
  Button: ({ children, onClick, disabled, type, ...rest }: React.ButtonHTMLAttributes<HTMLButtonElement> & { children?: React.ReactNode }) => (
    <button onClick={onClick} disabled={disabled} type={type ?? 'button'} {...rest}>{children}</button>
  ),
}));
vi.mock('@/components/ui/textarea', () => ({
  Textarea: (props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea {...props} />,
}));
vi.mock('@/components/ui/alert-dialog', () => ({
  AlertDialog: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AlertDialogTrigger: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AlertDialogContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AlertDialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AlertDialogTitle: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AlertDialogDescription: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AlertDialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AlertDialogCancel: ({ children }: { children: React.ReactNode }) => <button>{children}</button>,
  AlertDialogAction: ({ children, onClick }: { children?: React.ReactNode; onClick?: () => void }) => <button onClick={onClick}>{children}</button>,
}));
vi.mock('lucide-react', () => ({
  Send: () => null,
  Square: () => null,
  Trash2: () => null,
}));
vi.mock('@/components/ui/tooltip', () => ({
  TooltipProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  Tooltip: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipTrigger: ({ children, asChild }: { children: React.ReactNode; asChild?: boolean }) =>
    asChild ? <>{children}</> : <span>{children}</span>,
  TooltipContent: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="tooltip-content">{children}</div>
  ),
}));

import React from 'react';
const { StreamingChat } = await import('@/components/chat/StreamingChat');

function baseHookReturn(overrides: Record<string, unknown> = {}) {
  return {
    messages: [],
    sources: [],
    isStreaming: false,
    phase: 'idle',
    sendMessage: vi.fn(),
    stopStreaming: vi.fn(),
    clearChat: vi.fn(),
    modelUsed: null,
    streamError: null,
    ...overrides,
  };
}

describe('StreamingChat — FE-SSE-1 error banner', () => {
  beforeEach(() => {
    mockUseStreamingChat.mockReturnValue(baseHookReturn());
  });

  it('renders no alert when streamError is null', () => {
    render(<StreamingChat chatId="c1" scope="cross-paper" />);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('renders alert when streamError is set', () => {
    mockUseStreamingChat.mockReturnValue(baseHookReturn({ streamError: 'context too long' }));
    render(<StreamingChat chatId="c1" scope="cross-paper" />);
    const alert = screen.getByRole('alert');
    expect(alert).toBeTruthy();
    expect(alert.textContent).toBe('context too long');
  });

  it('threads elapsedSeconds and isFirstQuestion from the hook into the loading ChatMessage (U1)', () => {
    mockUseStreamingChat.mockReturnValue(
      baseHookReturn({
        isStreaming: true,
        phase: 'searching',
        elapsedSeconds: 6,
        messages: [
          { id: 'u1', role: 'user', content: 'first question' },
          { id: 'a1', role: 'assistant', content: '' },
        ],
      }),
    );
    render(<StreamingChat chatId="c1" scope="cross-paper" />);
    const loading = screen.getByTestId('chat-message-loading');
    expect(loading.getAttribute('data-elapsed')).toBe('6');
    expect(loading.getAttribute('data-first')).toBe('true');
  });
});

describe('StreamingChat — HIGH-FE-01 key stability', () => {
  beforeEach(() => {
    mockUseStreamingChat.mockReturnValue(baseHookReturn());
  });

  it('maintains DOM identity for the streaming message across token appends', () => {
    const initialMessages = [
      { role: 'user' as const, content: 'test query', sources: [] },
      { role: 'assistant' as const, content: 'Hel', sources: [] },
    ];
    mockUseStreamingChat.mockReturnValue(baseHookReturn({ messages: initialMessages }));

    const { container, rerender } = render(<StreamingChat chatId="c1" scope="cross-paper" />);

    // Capture the DOM node for the assistant message BEFORE the token append
    const beforeNode = container.querySelector('.space-y-4 > div:last-child');
    expect(beforeNode).toBeTruthy();

    // Simulate a streaming-token append: same role+index, growing content
    const appendedMessages = [
      { role: 'user' as const, content: 'test query', sources: [] },
      { role: 'assistant' as const, content: 'Hello', sources: [] },
    ];
    mockUseStreamingChat.mockReturnValue(baseHookReturn({ messages: appendedMessages }));
    rerender(<StreamingChat chatId="c1" scope="cross-paper" />);

    // Strict identity — same DOM node, not a remount caused by a content-keyed key
    const afterNode = container.querySelector('.space-y-4 > div:last-child');
    expect(afterNode).toBe(beforeNode);
  });
});

describe('StreamingChat — U2 model footer wording', () => {
  beforeEach(() => {
    mockUseStreamingChat.mockReturnValue(baseHookReturn());
  });

  it('does not render the alarming "fallback model" text when modelUsed is null', () => {
    render(<StreamingChat chatId="c1" scope="cross-paper" />);
    expect(screen.queryByText(/fallback model/i)).toBeNull();
  });

  it('renders neutral "Model: <name>" footer when modelUsed is set (not streaming)', () => {
    mockUseStreamingChat.mockReturnValue(baseHookReturn({ modelUsed: 'qwen3:4b', isStreaming: false }));
    render(<StreamingChat chatId="c1" scope="cross-paper" />);
    // Must contain the neutral label — no "fallback model" wording
    expect(screen.getByText(/^Model: qwen3:4b$/)).toBeInTheDocument();
    expect(screen.queryByText(/fallback model/i)).toBeNull();
  });

  it('hides the model footer while streaming', () => {
    mockUseStreamingChat.mockReturnValue(baseHookReturn({ modelUsed: 'qwen3:4b', isStreaming: true }));
    render(<StreamingChat chatId="c1" scope="cross-paper" />);
    expect(screen.queryByText(/Model:/)).toBeNull();
  });
});

describe('StreamingChat — Ask-gating tooltip', () => {
  beforeEach(() => {
    mockUseStreamingChat.mockReturnValue(baseHookReturn());
  });

  it('does not render the gating tooltip content when hasAnalyzedPapers is true (default)', () => {
    render(<StreamingChat chatId="c1" scope="cross-paper" />);
    expect(screen.queryByTestId('tooltip-content')).toBeNull();
  });

  it('renders the tooltip content "Process at least one paper first" when hasAnalyzedPapers is false', () => {
    render(<StreamingChat chatId="c1" scope="cross-paper" hasAnalyzedPapers={false} />);
    const tooltip = screen.getByTestId('tooltip-content');
    expect(tooltip).toBeTruthy();
    expect(tooltip.textContent).toBe('Process at least one paper first');
  });

  it('disables the textarea when hasAnalyzedPapers is false', () => {
    render(<StreamingChat chatId="c1" scope="cross-paper" hasAnalyzedPapers={false} />);
    const textarea = screen.getByPlaceholderText('Ask a question...');
    expect(textarea).toBeDisabled();
  });

  it('keeps textarea enabled when hasAnalyzedPapers is true', () => {
    render(<StreamingChat chatId="c1" scope="cross-paper" hasAnalyzedPapers={true} />);
    const textarea = screen.getByPlaceholderText('Ask a question...');
    expect(textarea).not.toBeDisabled();
  });
});
