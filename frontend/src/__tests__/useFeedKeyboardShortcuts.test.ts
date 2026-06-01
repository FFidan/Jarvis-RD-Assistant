import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { useFeedKeyboardShortcuts } from '@/hooks/useFeedKeyboardShortcuts';
import type { FeedKeyboardCallbacks } from '@/hooks/useFeedKeyboardShortcuts';
import type { LifecycleState } from '@/types';

function fireKey(key: string, extra: KeyboardEventInit = {}) {
  fireEvent.keyDown(window, { key, ...extra });
}

// Helper to build a minimal papers array with a selected index
function makePapers(state: LifecycleState = 'inbox') {
  return [{ id: 42, state }];
}

function renderShortcuts(
  state: LifecycleState,
  callbacks: FeedKeyboardCallbacks,
  selectedIndex: number | null = 0,
) {
  const papers = makePapers(state);
  return renderHook(() =>
    useFeedKeyboardShortcuts('inbox', papers, selectedIndex, callbacks),
  );
}

describe('useFeedKeyboardShortcuts', () => {
  let onNext: Mock<() => void>;
  let onPrev: Mock<() => void>;
  let onSave: Mock<(id: number) => void>;
  let onStar: Mock<(id: number) => void>;
  let onSaveAndStar: Mock<(id: number) => void>;
  let onSetAside: Mock<(id: number) => void>;
  let onTrash: Mock<(id: number) => void>;
  let onRestore: Mock<(id: number) => void>;
  let onMarkDone: Mock<(id: number) => void>;
  let onOpenDetail: Mock<(id: number) => void>;
  let onShowCheatSheet: Mock<() => void>;
  let onClearSelection: Mock<() => void>;

  beforeEach(() => {
    onNext = vi.fn<() => void>();
    onPrev = vi.fn<() => void>();
    onSave = vi.fn<(id: number) => void>();
    onStar = vi.fn<(id: number) => void>();
    onSaveAndStar = vi.fn<(id: number) => void>();
    onSetAside = vi.fn<(id: number) => void>();
    onTrash = vi.fn<(id: number) => void>();
    onRestore = vi.fn<(id: number) => void>();
    onMarkDone = vi.fn<(id: number) => void>();
    onOpenDetail = vi.fn<(id: number) => void>();
    onShowCheatSheet = vi.fn<() => void>();
    onClearSelection = vi.fn<() => void>();
  });

  // ── Navigation ────────────────────────────────────────────────────────────

  it('j fires onNext', () => {
    renderShortcuts('inbox', { onNext, onPrev });
    fireKey('j');
    expect(onNext).toHaveBeenCalledOnce();
  });

  it('k fires onPrev', () => {
    renderShortcuts('inbox', { onNext, onPrev });
    fireKey('k');
    expect(onPrev).toHaveBeenCalledOnce();
  });

  it('? fires onShowCheatSheet', () => {
    renderShortcuts('inbox', { onShowCheatSheet });
    fireKey('?');
    expect(onShowCheatSheet).toHaveBeenCalledOnce();
  });

  it('Escape fires onClearSelection', () => {
    renderShortcuts('inbox', { onClearSelection });
    fireKey('Escape');
    expect(onClearSelection).toHaveBeenCalledOnce();
  });

  // ── Open detail ───────────────────────────────────────────────────────────

  it('o fires onOpenDetail with paper id', () => {
    renderShortcuts('inbox', { onOpenDetail });
    fireKey('o');
    expect(onOpenDetail).toHaveBeenCalledOnce();
    expect(onOpenDetail).toHaveBeenCalledWith(42);
  });

  it('Enter fires onOpenDetail with paper id', () => {
    renderShortcuts('inbox', { onOpenDetail });
    fireKey('Enter');
    expect(onOpenDetail).toHaveBeenCalledOnce();
    expect(onOpenDetail).toHaveBeenCalledWith(42);
  });

  // ── s — state-aware ───────────────────────────────────────────────────────

  it('s on inbox-state paper fires onSave with id', () => {
    renderShortcuts('inbox', { onSave, onStar });
    fireKey('s');
    expect(onSave).toHaveBeenCalledOnce();
    expect(onSave).toHaveBeenCalledWith(42);
    expect(onStar).not.toHaveBeenCalled();
  });

  it('s on reading-state paper fires onStar (not onSave)', () => {
    renderShortcuts('reading', { onSave, onStar });
    fireKey('s');
    expect(onStar).toHaveBeenCalledOnce();
    expect(onStar).toHaveBeenCalledWith(42);
    expect(onSave).not.toHaveBeenCalled();
  });

  it('s on to_read-state paper fires onStar', () => {
    renderShortcuts('to_read', { onSave, onStar });
    fireKey('s');
    expect(onStar).toHaveBeenCalledOnce();
    expect(onStar).toHaveBeenCalledWith(42);
    expect(onSave).not.toHaveBeenCalled();
  });

  it('s on trash-state paper is a no-op', () => {
    renderShortcuts('trash', { onSave, onStar });
    fireKey('s');
    expect(onSave).not.toHaveBeenCalled();
    expect(onStar).not.toHaveBeenCalled();
  });

  // ── Shift+s ───────────────────────────────────────────────────────────────

  it('Shift+s on inbox fires onSaveAndStar with id', () => {
    renderShortcuts('inbox', { onSave, onSaveAndStar });
    fireKey('S', { shiftKey: true });
    expect(onSaveAndStar).toHaveBeenCalledOnce();
    expect(onSaveAndStar).toHaveBeenCalledWith(42);
    expect(onSave).not.toHaveBeenCalled();
  });

  it('Shift+s on non-inbox surface is a no-op', () => {
    renderShortcuts('reading', { onSaveAndStar });
    fireKey('S', { shiftKey: true });
    expect(onSaveAndStar).not.toHaveBeenCalled();
  });

  // ── t — trash ─────────────────────────────────────────────────────────────

  it('t on non-trash paper fires onTrash with id', () => {
    renderShortcuts('inbox', { onTrash });
    fireKey('t');
    expect(onTrash).toHaveBeenCalledOnce();
    expect(onTrash).toHaveBeenCalledWith(42);
  });

  it('t on trash-state paper is a no-op', () => {
    renderShortcuts('trash', { onTrash });
    fireKey('t');
    expect(onTrash).not.toHaveBeenCalled();
  });

  // ── e — set aside ─────────────────────────────────────────────────────────

  it('e on reading-state fires onSetAside with id', () => {
    renderShortcuts('reading', { onSetAside });
    fireKey('e');
    expect(onSetAside).toHaveBeenCalledOnce();
    expect(onSetAside).toHaveBeenCalledWith(42);
  });

  it('e on inbox-state is a no-op', () => {
    renderShortcuts('inbox', { onSetAside });
    fireKey('e');
    expect(onSetAside).not.toHaveBeenCalled();
  });

  it('e on to_read-state is a no-op', () => {
    renderShortcuts('to_read', { onSetAside });
    fireKey('e');
    expect(onSetAside).not.toHaveBeenCalled();
  });

  // ── r — restore ───────────────────────────────────────────────────────────

  it('r on trash-state fires onRestore with id', () => {
    renderShortcuts('trash', { onRestore });
    fireKey('r');
    expect(onRestore).toHaveBeenCalledOnce();
    expect(onRestore).toHaveBeenCalledWith(42);
  });

  it('r on non-trash state is a no-op', () => {
    renderShortcuts('inbox', { onRestore });
    fireKey('r');
    expect(onRestore).not.toHaveBeenCalled();
  });

  // ── d — done ──────────────────────────────────────────────────────────────

  it('d on reading-state fires onMarkDone with id', () => {
    renderShortcuts('reading', { onMarkDone });
    fireKey('d');
    expect(onMarkDone).toHaveBeenCalledOnce();
    expect(onMarkDone).toHaveBeenCalledWith(42);
  });

  it('d on to_read-state fires onMarkDone with id', () => {
    renderShortcuts('to_read', { onMarkDone });
    fireKey('d');
    expect(onMarkDone).toHaveBeenCalledOnce();
    expect(onMarkDone).toHaveBeenCalledWith(42);
  });

  it('d on inbox-state is a no-op', () => {
    renderShortcuts('inbox', { onMarkDone });
    fireKey('d');
    expect(onMarkDone).not.toHaveBeenCalled();
  });

  // ── No selected paper ─────────────────────────────────────────────────────

  it('paper-specific shortcuts do nothing when selectedIndex is null', () => {
    renderHook(() =>
      useFeedKeyboardShortcuts('inbox', makePapers('inbox'), null, {
        onSave, onStar, onTrash, onOpenDetail,
      }),
    );
    fireKey('s');
    fireKey('t');
    fireKey('o');
    expect(onSave).not.toHaveBeenCalled();
    expect(onStar).not.toHaveBeenCalled();
    expect(onTrash).not.toHaveBeenCalled();
    expect(onOpenDetail).not.toHaveBeenCalled();
  });

  // ── Ignored events ────────────────────────────────────────────────────────

  it('keyboard ignored when an input element is focused', () => {
    renderShortcuts('inbox', { onNext });
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    fireEvent.keyDown(input, { key: 'j' });
    expect(onNext).not.toHaveBeenCalled();
    document.body.removeChild(input);
  });

  it('modifier key metaKey + j is ignored', () => {
    renderShortcuts('inbox', { onNext });
    fireKey('j', { metaKey: true });
    expect(onNext).not.toHaveBeenCalled();
  });

  it('modifier key ctrlKey + j is ignored', () => {
    renderShortcuts('inbox', { onNext });
    fireKey('j', { ctrlKey: true });
    expect(onNext).not.toHaveBeenCalled();
  });

  it('modifier key altKey + j is ignored', () => {
    renderShortcuts('inbox', { onNext });
    fireKey('j', { altKey: true });
    expect(onNext).not.toHaveBeenCalled();
  });

  // ── M17 — ref-stability ───────────────────────────────────────────────────

  describe('M17 — ref-stability: listener registered exactly once', () => {
    let addSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
      addSpy = vi.spyOn(window, 'addEventListener');
    });

    afterEach(() => {
      addSpy.mockRestore();
    });

    it('addEventListener("keydown") is called exactly once even after re-renders with new callbacks', () => {
      const papers = makePapers('inbox');
      const { rerender } = renderHook(
        ({ callbacks }: { callbacks: FeedKeyboardCallbacks }) =>
          useFeedKeyboardShortcuts('inbox', papers, 0, callbacks),
        { initialProps: { callbacks: { onNext: vi.fn<() => void>() } } },
      );

      act(() => { rerender({ callbacks: { onNext: vi.fn<() => void>() } }); });
      act(() => { rerender({ callbacks: { onNext: vi.fn<() => void>() } }); });

      const keydownCalls = addSpy.mock.calls.filter(([event]: [string, ...unknown[]]) => event === 'keydown');
      expect(keydownCalls).toHaveLength(1);
    });

    it('latest callbacks are respected after re-renders', () => {
      const firstOnNext = vi.fn<() => void>();
      const latestOnNext = vi.fn<() => void>();
      const papers = makePapers('inbox');

      const { rerender } = renderHook(
        ({ callbacks }: { callbacks: FeedKeyboardCallbacks }) =>
          useFeedKeyboardShortcuts('inbox', papers, 0, callbacks),
        { initialProps: { callbacks: { onNext: firstOnNext } } },
      );

      act(() => { rerender({ callbacks: { onNext: latestOnNext } }); });

      fireKey('j');
      expect(latestOnNext).toHaveBeenCalledOnce();
      expect(firstOnNext).not.toHaveBeenCalled();
    });

    it('keyboard event after hook unmount does NOT fire any callback', () => {
      const { unmount } = renderShortcuts('inbox', { onNext, onSave, onTrash });

      unmount();

      fireKey('j');
      fireKey('s');
      fireKey('t');

      expect(onNext).not.toHaveBeenCalled();
      expect(onSave).not.toHaveBeenCalled();
      expect(onTrash).not.toHaveBeenCalled();
    });
  });
});
