import { useState } from 'react';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { TimeSelect } from '@/components/ui/time-select';
import { cronToTime, splitCron } from '@/lib/cron-utils';

interface ScheduleSelectProps {
  value: string;
  onChange: (cron: string) => void;
  disabled?: boolean;
  id?: string;
}

/** Step sizes offered for the "every N hours" preset. */
const HOUR_STEPS = [2, 3, 4, 6, 8, 12];
/** The only minute values `TimeSelect` can display and round-trip. */
const TIME_SELECT_MINUTES = new Set(['00', '15', '30', '45']);

const DAYS: { value: string; label: string }[] = [
  { value: '0', label: 'Sunday' },
  { value: '1', label: 'Monday' },
  { value: '2', label: 'Tuesday' },
  { value: '3', label: 'Wednesday' },
  { value: '4', label: 'Thursday' },
  { value: '5', label: 'Friday' },
  { value: '6', label: 'Saturday' },
];

/** Fallbacks used only when switching a preset that had no prior value to carry forward. */
const DEFAULT_TIME = '09:00';
const DEFAULT_DAY = '1';
const DEFAULT_HOUR_STEP = 4;

type Schedule =
  | { kind: 'hourly' }
  | { kind: 'every-n-hours'; hours: number }
  | { kind: 'daily'; time: string }
  | { kind: 'weekly'; time: string; day: string }
  | { kind: 'custom' };

/**
 * Recognise a 5-field cron expression as one of the four presets this
 * control renders, or report it as custom. Custom is the safe default for
 * anything the control cannot faithfully render and reproduce: a wrong
 * guess here would silently rewrite the caller's schedule the moment the
 * user touches an unrelated control.
 */
function parseSchedule(cron: string): Schedule {
  let parts: [string, string, string, string, string];
  try {
    parts = splitCron(cron);
  } catch {
    return { kind: 'custom' };
  }
  const [minute, hour, dom, month, dow] = parts;
  if (dom !== '*' || month !== '*') return { kind: 'custom' };

  if (Number(minute) === 0 && hour === '*' && dow === '*') {
    return { kind: 'hourly' };
  }

  const stepMatch = /^\*\/(\d+)$/.exec(hour);
  if (Number(minute) === 0 && stepMatch && dow === '*') {
    const hours = parseInt(stepMatch[1] ?? '', 10);
    return HOUR_STEPS.includes(hours) ? { kind: 'every-n-hours', hours } : { kind: 'custom' };
  }

  const time = toTimeSelectValue(hour, minute);
  if (time === null) return { kind: 'custom' };

  if (dow === '*') return { kind: 'daily', time };
  if (/^[0-6]$/.test(dow)) return { kind: 'weekly', time, day: dow };
  return { kind: 'custom' };
}

/**
 * Convert cron hour/minute fields to the "HH:MM" shape `TimeSelect` can
 * display, or null when the fields fall outside what its dropdowns offer
 * (24 hours, but only :00/:15/:30/:45).
 */
function toTimeSelectValue(hour: string, minute: string): string | null {
  const h = Number(hour);
  const m = Number(minute);
  if (!Number.isInteger(h) || h < 0 || h > 23 || !Number.isInteger(m)) return null;
  const time = cronToTime(`${m} ${h} * * *`);
  const [, mm = ''] = time.split(':');
  return TIME_SELECT_MINUTES.has(mm) ? time : null;
}

export function ScheduleSelect({ value, onChange, disabled, id }: ScheduleSelectProps) {
  const schedule = parseSchedule(value);
  const frequencyId = id ? `${id}-frequency` : 'schedule-frequency';

  // Remember the run time, day and hour-step the control last actually held.
  // Only some presets carry each field, so leaving one and coming back would
  // otherwise fall through to the constant defaults below, silently rewriting
  // whatever value was there before. Daily and weekly are the presets that
  // carry a time, so it needs the same memory the day and hour-step already have.
  const timePreset = schedule.kind === 'daily' || schedule.kind === 'weekly';
  const [lastTime, setLastTime] = useState(timePreset ? schedule.time : DEFAULT_TIME);
  if (timePreset && schedule.time !== lastTime) {
    setLastTime(schedule.time);
  }
  const currentTime = timePreset ? schedule.time : lastTime;

  const [lastDay, setLastDay] = useState(schedule.kind === 'weekly' ? schedule.day : DEFAULT_DAY);
  if (schedule.kind === 'weekly' && schedule.day !== lastDay) {
    setLastDay(schedule.day);
  }
  const currentDay = schedule.kind === 'weekly' ? schedule.day : lastDay;

  const [lastHours, setLastHours] = useState(
    schedule.kind === 'every-n-hours' ? schedule.hours : DEFAULT_HOUR_STEP,
  );
  if (schedule.kind === 'every-n-hours' && schedule.hours !== lastHours) {
    setLastHours(schedule.hours);
  }
  const currentHours = schedule.kind === 'every-n-hours' ? schedule.hours : lastHours;

  function handleFrequencyChange(frequency: string) {
    const [hh, mm] = currentTime.split(':');
    switch (frequency) {
      case 'hourly':
        onChange('0 * * * *');
        return;
      case 'every-n-hours':
        onChange(`0 */${currentHours} * * *`);
        return;
      case 'daily':
        onChange(`${Number(mm)} ${Number(hh)} * * *`);
        return;
      case 'weekly':
        onChange(`${Number(mm)} ${Number(hh)} * * ${currentDay}`);
        return;
    }
  }

  function handleHoursChange(hours: string) {
    onChange(`0 */${hours} * * *`);
  }

  function handleTimeChange(time: string) {
    const [hh, mm] = time.split(':');
    if (schedule.kind === 'weekly') {
      onChange(`${Number(mm)} ${Number(hh)} * * ${currentDay}`);
    } else {
      onChange(`${Number(mm)} ${Number(hh)} * * *`);
    }
  }

  function handleDayChange(day: string) {
    const [hh, mm] = currentTime.split(':');
    onChange(`${Number(mm)} ${Number(hh)} * * ${day}`);
  }

  return (
    <div className="flex flex-wrap items-end gap-2">
      <div>
        <Label htmlFor={frequencyId} className="mb-1 block text-sm font-medium">
          Frequency
        </Label>
        <Select
          value={schedule.kind === 'custom' ? '' : schedule.kind}
          onValueChange={handleFrequencyChange}
          disabled={disabled}
        >
          <SelectTrigger id={frequencyId} className="w-44">
            <SelectValue placeholder="Custom" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="hourly">
              Every hour
            </SelectItem>
            <SelectItem value="every-n-hours">
              Every N hours
            </SelectItem>
            <SelectItem value="daily">
              Daily
            </SelectItem>
            <SelectItem value="weekly">
              Weekly
            </SelectItem>
          </SelectContent>
        </Select>
      </div>

      {schedule.kind === 'every-n-hours' && (
        <Select value={String(currentHours)} onValueChange={handleHoursChange} disabled={disabled}>
          <SelectTrigger className="w-36" aria-label="Hours">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {HOUR_STEPS.map((hours) => (
              <SelectItem key={hours} value={String(hours)}>
                Every {hours} hours
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}

      {(schedule.kind === 'daily' || schedule.kind === 'weekly') && (
        <div role="group" aria-label="Run time">
          <Label className="mb-1 block text-sm font-medium">Run time</Label>
          <TimeSelect value={currentTime} onChange={handleTimeChange} disabled={disabled} />
        </div>
      )}

      {schedule.kind === 'weekly' && (
        <Select value={currentDay} onValueChange={handleDayChange} disabled={disabled}>
          <SelectTrigger className="w-36" aria-label="Day of week">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {DAYS.map((day) => (
              <SelectItem key={day.value} value={day.value}>
                {day.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </div>
  );
}
