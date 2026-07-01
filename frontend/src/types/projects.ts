export type ProjectStatus = 'active' | 'paused' | 'completed' | 'archived';

export type TaskStatus = 'todo' | 'in_progress' | 'done' | 'blocked';

export interface Project {
  id: number;
  name: string;
  description: string | null;
  status: ProjectStatus;
  deadline: string | null;
  color: string | null;
  next_milestone?: string | null;
  next_milestone_due?: string | null;
  created_at: string;
  updated_at: string;
  paper_count?: number;
  open_question_count?: number;
}

export interface ProjectQuestion {
  id: number;
  project_id: number;
  body: string;
  created_at: string;
}

export interface ProjectActivityItem {
  kind: 'added_paper' | 'completed_task' | 'completed_milestone';
  ts: string;
  label: string;
}

export interface Task {
  id: number;
  project_id: number | null;
  parent_task_id: number | null;
  title: string;
  description: string | null;
  status: TaskStatus;
  priority: number;
  deadline: string | null;
  estimated_hours: number | null;
  actual_hours: number | null;
  sort_order: number;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Milestone {
  id: number;
  project_id: number;
  name: string;
  deadline: string | null;
  description: string | null;
  completed: boolean;
  completed_at: string | null;
  created_at: string;
}
