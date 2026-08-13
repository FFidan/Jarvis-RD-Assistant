import type { TaskStatus } from './projects';

export interface DashboardMetrics {
  total_papers: number;
  unread_papers: number;
  pending_papers: number;
  due_cards: number;
  active_projects: number;
  topic_count: number;
  nudge_count: number;
  chunked_papers?: number;
  onboarding_stage?: string;
}

export interface MyDayTask {
  id: number;
  project_id: number | null;
  title: string;
  priority: number;
  deadline: string | null;
  status: TaskStatus;
  completed_at: string | null;
  project_name: string | null;
  project_color: string | null;
}

export interface ProjectPulseItem {
  id: number;
  name: string;
  color: string | null;
  total_tasks: number;
  done_tasks: number;
  next_milestone: string | null;
  next_milestone_deadline: string | null;
}

export interface MyDayResponse {
  tasks: MyDayTask[];
  cards_due: number;
  recommendations: Array<{
    recommendation_id: number;
    paper_id: number;
    score: number;
    title: string;
    authors: string[];
  }>;
  today_focus_hours: number;
  focus_streak_days: number;
  project_pulse: ProjectPulseItem[];
}

export interface ActiveFocusSession {
  id: number;
  state: 'active' | 'paused' | 'completed';
  source: 'web' | 'telegram';
  duration_seconds: number;
  remaining_seconds: number;
  started_at: string;
  paused_at: string | null;
  paused_seconds: number;
  completed_at: string | null;
  recorded_seconds: number;
  task_id: number | null;
  paper_id: number | null;
}

export interface FocusSessionTransition {
  session: ActiveFocusSession;
  changed: boolean;
}

export interface YesterdayTask {
  id: number;
  title: string;
  status: TaskStatus;
}

export interface YesterdaySummary {
  date: string; // ISO date YYYY-MM-DD
  focused_hours: number;
  cards_reviewed: number;
  tasks_done: number;
  completed: YesterdayTask[];
  deferred: YesterdayTask[];
}

export interface Thread {
  id: number;
  title: string;
  anchor: string | null;
  progress: number; // 0..1
  last_at: string;
  status: 'open' | 'done' | 'archived';
  created_at: string;
}

export interface ThreadSeedResponse {
  thread: Thread;
  created: boolean;
}

export interface JournalPrompts {
  first_move?: string | null;
  worked?: string | null;
  blocked?: string | null;
  note?: string | null;
}

export interface JournalEntry {
  id: number;
  date: string; // ISO date YYYY-MM-DD
  prompts: JournalPrompts;
  created_at: string;
  updated_at: string;
}

export interface MyDayBundle {
  tasks: MyDayTask[];
  intent: { intent: string | null; updated_at: string | null };
  threads: Array<Omit<Thread, 'last_at'> & { last_at: string | null }>;
  yesterday: YesterdaySummary;
  journal: JournalEntry | null;
}
