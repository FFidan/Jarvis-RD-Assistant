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
    ['0 * * * *', 'Every hour', 'Weekly'],
    ['0 */12 * * *', 'Every N hours', 'Weekly'],
    ['30 7 * * *', 'Daily', 'Weekly'],
    ['0 9 * * 5', 'Weekly', 'Daily'],
  ])('gives %s back unchanged after switching away and back', async (cron, label, away) => {
    // Two real changes rather than re-picking the option already shown: the
    // underlying Select correctly ignores a pick that changes nothing, and a
    // no-op selection must not write. Leaving and returning still exercises
    // parse -> render -> serialize, and pins that the value survives the trip.
    // The hour-step and weekday cases deliberately use non-default values (12
    // hours, Friday) so the round trip fails if the control forgets the value
    // it last held instead of restoring it.
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
    await userEvent.click(await screen.findByRole('option', { name: new RegExp(`^${away}$`, 'i') }));

    await userEvent.click(screen.getByRole('combobox', { name: /frequency/i }));
    await userEvent.click(await screen.findByRole('option', { name: new RegExp(`^${label}$`, 'i') }));

    expect(seen).toHaveLength(2);
    expect(seen[seen.length - 1]).toBe(cron);
  });

  it('remembers the run time across a detour through a preset without one', async () => {
    // Daily 14:00 -> Every hour (no time field) -> Daily must restore 14:00, not
    // fall back to the 09:00 default. lastDay/lastHours already do this for their
    // fields; without the same memory for the time, returning silently rewrites it.
    const seen: string[] = [];
    function Harness() {
      const [value, setValue] = useState('0 14 * * *');
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
    await userEvent.click(await screen.findByRole('option', { name: /^Every hour$/i }));

    await userEvent.click(screen.getByRole('combobox', { name: /frequency/i }));
    await userEvent.click(await screen.findByRole('option', { name: /^Daily$/i }));

    expect(seen[seen.length - 1]).toBe('0 14 * * *');
  });

  it('gives the hour-step select an accessible name', () => {
    render(<ScheduleSelect value="0 */4 * * *" onChange={vi.fn()} />);
    expect(screen.getByRole('combobox', { name: /hours/i })).toBeInTheDocument();
  });

  it('gives the weekday select and the time picker an accessible name', () => {
    const { container } = render(<ScheduleSelect value="0 9 * * 1" onChange={vi.fn()} />);
    expect(screen.getByRole('combobox', { name: /day of week/i })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: /run time/i })).toBeInTheDocument();
    expect(screen.getByText('Run time')).toBeInTheDocument();
    expect(container.firstElementChild).toHaveClass('items-end');
  });

  it('reports an unrecognised schedule as custom and writes nothing', () => {
    const onChange = vi.fn();
    render(<ScheduleSelect value="15 3 1 * 2" onChange={onChange} />);
    expect(screen.getByText(/custom/i)).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });
});
