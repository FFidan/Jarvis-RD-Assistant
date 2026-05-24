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
  ChatMessage: () => null,
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
  AlertTriangle: () => null,
  Send: () => null,
  Square: () => null,
  Trash2: () => null,
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
});
