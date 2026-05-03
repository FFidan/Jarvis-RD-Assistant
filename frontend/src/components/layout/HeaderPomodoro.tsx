import { useNavigate } from 'react-router-dom';
import { Pause, Play } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { usePomodoroStore } from '@/stores/pomodoro-store';

type TimerPhase = 'idle' | 'work' | 'short-break' | 'long-break';

const PHASE_LABELS: Record<TimerPhase, string> = {
  idle: 'Idle',
  work: 'Focus',
  'short-break': 'Short Break',
  'long-break': 'Long Break',
};

const PHASE_COLORS: Record<TimerPhase, string> = {
  idle: '',
  work: 'text-red-500',
  'short-break': 'text-amber-500',
  'long-break': 'text-green-500',
};

function formatMMSS(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

export function HeaderPomodoro() {
  const phase = usePomodoroStore((s) => s.phase);
  const secondsRemaining = usePomodoroStore((s) => s.secondsRemaining);
  const pausedAt = usePomodoroStore((s) => s.pausedAt);
  const pause = usePomodoroStore((s) => s.pause);
  const resume = usePomodoroStore((s) => s.resume);
  const attachedItem = usePomodoroStore((s) => s.attachedItem);
  const navigate = useNavigate();

  if (phase === 'idle') return null;

  const isPaused = pausedAt !== null;
  const colorClass = PHASE_COLORS[phase];
  const phaseLabel = PHASE_LABELS[phase];
  const tooltipText = `Pomodoro · ${phaseLabel} · click to open`;

  const handleTimeClick = () => {
    navigate('/my-day#now');
  };

  const handleTogglePause = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (isPaused) {
      resume();
    } else {
      pause();
    }
  };

  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <div className="flex items-center gap-1 rounded-full border bg-muted/50 px-3 py-1 text-sm">
            <button
              onClick={handleTimeClick}
              className={`font-mono tabular-nums tracking-tight font-semibold ${colorClass} hover:opacity-80 transition-opacity`}
              aria-label={`Pomodoro ${phaseLabel} - ${formatMMSS(secondsRemaining)} remaining, click to open`}
            >
              {formatMMSS(secondsRemaining)}
            </button>
            {attachedItem?.title && (
              <span className="text-[10px] text-meta dark:text-zinc-400 max-w-[120px] truncate">
                {attachedItem.title}
              </span>
            )}
            <Button
              variant="ghost"
              size="icon"
              className="h-5 w-5"
              onClick={handleTogglePause}
              aria-label={isPaused ? 'Resume Pomodoro' : 'Pause Pomodoro'}
            >
              {isPaused ? (
                <Play className="h-3 w-3" />
              ) : (
                <Pause className="h-3 w-3" />
              )}
            </Button>
          </div>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          <p>{tooltipText}</p>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
