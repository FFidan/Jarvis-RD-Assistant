import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { useFeedKeyboardShortcuts } from '@/hooks/useFeedKeyboardShortcuts';

function fireKey(key: string, extra: KeyboardEventInit = {}) {
  fireEvent.keyDown(window, { key, ...extra });
}

describe('useFeedKeyboardShortcuts', () => {
  let onNext: ReturnType<typeof vi.fn>;
  let onPrev: ReturnType<typeof vi.fn>;
  let onSave: ReturnType<typeof vi.fn>;
  let onStar: ReturnType<typeof vi.fn>;
  let onSaveAndStar: ReturnType<typeof vi.fn>;
  let onArchive: ReturnType<typeof vi.fn>;
  let onDismiss: ReturnType<typeof vi.fn>;
  let onMarkRead: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    onNext = vi.fn();
    onPrev = vi.fn();
    onSave = vi.fn();
    onStar = vi.fn();
    onSaveAndStar = vi.fn();
    onArchive = vi.fn();
    onDismiss = vi.fn();
    onMarkRead = vi.fn();
  });

  it('j calls onNext', () => {
    renderHook(() =>
      useFeedKeyboardShortcuts('inbox', { onNext, onPrev }),
    );
    fireKey('j');
    expect(onNext).toHaveBeenCalledOnce();
  });

  it('k calls onPrev', () => {
    renderHook(() =>
      useFeedKeyboardShortcuts('inbox', { onNext, onPrev }),
    );
    fireKey('k');
    expect(onPrev).toHaveBeenCalledOnce();
  });

  it('s on inbox surface calls onSave (not onStar)', () => {
    renderHook(() =>
      useFeedKeyboardShortcuts('inbox', { onSave, onStar }),
    );
    fireKey('s');
    expect(onSave).toHaveBeenCalledOnce();
    expect(onStar).not.toHaveBeenCalled();
  });

  it('s on library surface calls onStar (not onSave)', () => {
    renderHook(() =>
      useFeedKeyboardShortcuts('library', { onSave, onStar }),
    );
    fireKey('s');
    expect(onStar).toHaveBeenCalledOnce();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('S (shift+s) calls onSaveAndStar', () => {
    renderHook(() =>
      useFeedKeyboardShortcuts('inbox', { onSave, onSaveAndStar }),
    );
    fireKey('s', { shiftKey: true });
    expect(onSaveAndStar).toHaveBeenCalledOnce();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('e on library surface calls onArchive', () => {
    renderHook(() =>
      useFeedKeyboardShortcuts('library', { onArchive }),
    );
    fireKey('e');
    expect(onArchive).toHaveBeenCalledOnce();
  });

  it('e on inbox surface does NOT call onArchive', () => {
    renderHook(() =>
      useFeedKeyboardShortcuts('inbox', { onArchive }),
    );
    fireKey('e');
    expect(onArchive).not.toHaveBeenCalled();
  });

  it('keyboard ignored when an input element is focused', () => {
    renderHook(() =>
      useFeedKeyboardShortcuts('inbox', { onNext }),
    );
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    fireEvent.keyDown(input, { key: 'j' });
    expect(onNext).not.toHaveBeenCalled();
    document.body.removeChild(input);
  });

  it('modifier key metaKey + j is ignored', () => {
    renderHook(() =>
      useFeedKeyboardShortcuts('inbox', { onNext }),
    );
    fireKey('j', { metaKey: true });
    expect(onNext).not.toHaveBeenCalled();
  });

  it('modifier key ctrlKey + j is ignored', () => {
    renderHook(() =>
      useFeedKeyboardShortcuts('inbox', { onNext }),
    );
    fireKey('j', { ctrlKey: true });
    expect(onNext).not.toHaveBeenCalled();
  });

  it('modifier key altKey + j is ignored', () => {
    renderHook(() =>
      useFeedKeyboardShortcuts('inbox', { onNext }),
    );
    fireKey('j', { altKey: true });
    expect(onNext).not.toHaveBeenCalled();
  });

  describe('M17 — ref-stability: listener registered exactly once', () => {
    let addSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
      addSpy = vi.spyOn(window, 'addEventListener');
    });

    afterEach(() => {
      addSpy.mockRestore();
    });

    it('addEventListener("keydown") is called exactly once even after re-renders with a new callbacks object', () => {
      const { rerender } = renderHook(
        ({ callbacks }: { callbacks: Record<string, () => void> }) =>
          useFeedKeyboardShortcuts('inbox', callbacks),
        { initialProps: { callbacks: { onNext: vi.fn() } } },
      );

      // Re-render with a brand-new callbacks object (different identity each time)
      act(() => {
        rerender({ callbacks: { onNext: vi.fn() } });
      });
      act(() => {
        rerender({ callbacks: { onNext: vi.fn() } });
      });

      const keydownCalls = addSpy.mock.calls.filter(
        ([event]) => event === 'keydown',
      );
      expect(keydownCalls).toHaveLength(1);
    });

    it('latest callbacks are still called after re-renders', () => {
      const firstOnNext = vi.fn();
      const latestOnNext = vi.fn();

      const { rerender } = renderHook(
        ({ callbacks }: { callbacks: Record<string, () => void> }) =>
          useFeedKeyboardShortcuts('inbox', callbacks),
        { initialProps: { callbacks: { onNext: firstOnNext } } },
      );

      act(() => {
        rerender({ callbacks: { onNext: latestOnNext } });
      });

      fireKey('j');
      expect(latestOnNext).toHaveBeenCalledOnce();
      expect(firstOnNext).not.toHaveBeenCalled();
    });
  });
});
