import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Slider } from '@/components/ui/slider';
import { usePomodoroStore } from '@/stores/pomodoro-store';

export function TimerSection() {
  const workMinutes = usePomodoroStore(s => s.workMinutes);
  const shortBreakMinutes = usePomodoroStore(s => s.shortBreakMinutes);
  const longBreakMinutes = usePomodoroStore(s => s.longBreakMinutes);
  const targetCycles = usePomodoroStore(s => s.targetCycles);
  const phase = usePomodoroStore(s => s.phase);
  const isActive = phase !== 'idle';

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardHeader>
        <p className="text-sm text-muted-foreground">
          Customize your focus and break durations.
        </p>
        {isActive && (
          <p className="text-sm text-yellow-600 dark:text-yellow-400 mt-1">
            Changes will apply to your next session.
          </p>
        )}
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="space-y-2">
          <Label>Work duration: {workMinutes} min</Label>
          <Slider
            aria-label="Work duration"
            min={15} max={60} step={5}
            value={[workMinutes]}
            onValueChange={([v]) => usePomodoroStore.setState({ workMinutes: v })}
          />
          <p className="text-xs text-muted-foreground">How long each focus session lasts (15–60 minutes)</p>
        </div>
        <div className="space-y-2">
          <Label>Short break: {shortBreakMinutes} min</Label>
          <Slider
            aria-label="Short break"
            min={3} max={15} step={1}
            value={[shortBreakMinutes]}
            onValueChange={([v]) => usePomodoroStore.setState({ shortBreakMinutes: v })}
          />
          <p className="text-xs text-muted-foreground">Break between work sessions (3–15 minutes)</p>
        </div>
        <div className="space-y-2">
          <Label>Long break: {longBreakMinutes} min</Label>
          <Slider
            aria-label="Long break"
            min={10} max={30} step={5}
            value={[longBreakMinutes]}
            onValueChange={([v]) => usePomodoroStore.setState({ longBreakMinutes: v })}
          />
          <p className="text-xs text-muted-foreground">Extended break after completing all cycles (10–30 minutes)</p>
        </div>
        <div className="space-y-2">
          <Label>Cycles before long break: {targetCycles}</Label>
          <Slider
            aria-label="Cycles before long break"
            min={2} max={8} step={1}
            value={[targetCycles]}
            onValueChange={([v]) => usePomodoroStore.setState({ targetCycles: v })}
          />
          <p className="text-xs text-muted-foreground">Number of work sessions before a long break (2–8)</p>
        </div>
      </CardContent>
    </Card>
  );
}
