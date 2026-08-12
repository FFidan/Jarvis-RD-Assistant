import { useState } from 'react';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ScheduleSelect } from '@/components/ui/schedule-select';

// Radix UI Select uses pointer-capture and scrollIntoView APIs not present in jsdom.
// Polyfill them globally so Select interaction tests can open the dropdown and pick options.
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
  if (!window.HTMLElement.prototype.scrollIntoView) {
    window.HTMLElement.prototype.scrollIntoView = () => {};
  }
});

describe('ScheduleSelect', () => {
  it.each(['0 * * * *', '0 */4 * * *', '30 7 * * *', '0 9 * * 1'])(
    'round-trips %s without changing it',
    (cron) => {
      const onChange = vi.fn();
      render(<ScheduleSelect value={cron} onChange={onChange} />);
      expect(onChange).not.toHaveBeenCalled();
    },
  );

  it.each([
    ['0 * * * *', 'Every hour'],
    ['30 7 * * *', 'Daily'],
  ])('gives %s back unchanged after switching away and back', async (cron, label) => {
    // Two real changes rather than re-picking the option already shown: the
    // underlying Select correctly ignores a pick that changes nothing, and a
    // no-op selection must not write. Leaving and returning still exercises
    // parse -> render -> serialize, and pins that the value survives the trip.
    const seen: string[] = [];
    function Harness() {
      const [value, setValue] = useState(cron);
      return (
        <ScheduleSelect
          value={value}
          onChange={(next) => {
            seen.push(next);
            setValue(next);
          }}
        />
      );
    }
    render(<Harness />);

    await userEvent.click(screen.getByRole('combobox', { name: /frequency/i }));
    await userEvent.click(await screen.findByRole('option', { name: /^weekly$/i }));

    await userEvent.click(screen.getByRole('combobox', { name: /frequency/i }));
    await userEvent.click(await screen.findByRole('option', { name: new RegExp(`^${label}$`, 'i') }));

    expect(seen).toHaveLength(2);
    expect(seen[seen.length - 1]).toBe(cron);
  });

  it('reports an unrecognised schedule as custom and writes nothing', () => {
    const onChange = vi.fn();
    render(<ScheduleSelect value="15 3 1 * 2" onChange={onChange} />);
    expect(screen.getByText(/custom/i)).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });
});
