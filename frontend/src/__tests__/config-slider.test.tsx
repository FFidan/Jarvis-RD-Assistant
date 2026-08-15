/**
 * ConfigSlider — the commit path must survive a late server value.
 *
 * Settings cards render with a fallback while the config request is in flight
 * and reset to the server value when it lands. If the control keeps treating
 * the fallback as "what the server has", dragging back to the fallback looks
 * like no change: nothing is sent, no toast appears, and the screen disagrees
 * with the server from then on.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import { ConfigSlider } from '@/components/ui/config-slider';
import { useSyncedState } from '@/components/settings/pulse/use-synced-state';

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

const FALLBACK = 10;
const SERVER = 20;
const STEP = 10;

/** Mirrors how the settings cards drive the control. */
function Harness({
  serverValue,
  onCommit,
}: {
  serverValue: number;
  onCommit: (v: number) => void;
}) {
  const [value, setValue] = useSyncedState(serverValue);
  return (
    <ConfigSlider
      label="Deck size"
      value={value}
      min={0}
      max={30}
      step={STEP}
      onLocalChange={setValue}
      onCommit={onCommit}
    />
  );
}

beforeAll(() => {
  if (!window.HTMLElement.prototype.hasPointerCapture) {
    window.HTMLElement.prototype.hasPointerCapture = () => false;
  }
  if (!window.HTMLElement.prototype.setPointerCapture) {
    window.HTMLElement.prototype.setPointerCapture = () => {};
  }
  if (!window.HTMLElement.prototype.releasePointerCapture) {
    window.HTMLElement.prototype.releasePointerCapture = () => {};
  }
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe('ConfigSlider', () => {
  it('commits a move back to the mount-time fallback once the server value has arrived', async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    const { rerender } = render(<Harness serverValue={FALLBACK} onCommit={onCommit} />);

    // The real config lands and replaces the fallback.
    rerender(<Harness serverValue={SERVER} onCommit={onCommit} />);
    const thumb = screen.getByRole('slider');
    expect(thumb).toHaveAttribute('aria-valuenow', String(SERVER));

    // The reader drags back to the value the control happened to mount with.
    thumb.focus();
    await user.keyboard('{ArrowDown}');

    expect(thumb).toHaveAttribute('aria-valuenow', String(FALLBACK));
    expect(onCommit).toHaveBeenCalledWith(FALLBACK);
    // And the Undo offered is the value actually replaced, not the fallback.
    expect(vi.mocked(toast.success)).toHaveBeenCalledWith(
      'Deck size saved: 10',
      expect.objectContaining({
        action: expect.objectContaining({ label: 'Undo (20)' }),
      }),
    );
  });
});
