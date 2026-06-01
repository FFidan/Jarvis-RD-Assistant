// Projects, open questions, recent activity, tasks, milestones, and the
// project↔paper link surface.
import { apiFetch } from './core';
import type {
  Project,
  Task,
  Milestone,
  ProjectPaper,
  Paper,
  ProjectQuestion,
  ProjectActivityItem,
} from '@/types';

// --- Projects ---
export const fetchProjects = (status?: string) =>
  apiFetch<Project[]>(`/api/projects${status ? `?status=${status}` : ''}`);
export const createProject = (data: {
  name: string;
  description?: string | null;
  status?: string;
  deadline?: string | null;
}) => apiFetch<Project>('/api/projects', { method: 'POST', body: JSON.stringify(data) });
export const updateProject = (id: number, data: Partial<Project>) =>
  apiFetch<Project>(`/api/projects/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteProject = (id: number) =>
  apiFetch<void>(`/api/projects/${id}`, { method: 'DELETE' });

// --- Project Open Questions (UI_v3 Projects § OPEN QUESTIONS) ---
export const fetchProjectQuestions = (projectId: number) =>
  apiFetch<ProjectQuestion[]>(`/api/projects/${projectId}/questions`);
export const createProjectQuestion = (projectId: number, body: string) =>
  apiFetch<ProjectQuestion>(`/api/projects/${projectId}/questions`, {
    method: 'POST',
    body: JSON.stringify({ body }),
  });
/** DELETE is addressed by question id (own /api/questions prefix). */
export const deleteProjectQuestion = (questionId: number) =>
  apiFetch<void>(`/api/questions/${questionId}`, { method: 'DELETE' });

// --- Project Recent Activity (UI_v3 Projects § RECENT ACTIVITY) ---
export const fetchProjectActivity = (projectId: number, limit?: number) =>
  apiFetch<ProjectActivityItem[]>(
    `/api/projects/${projectId}/activity${limit ? `?limit=${limit}` : ''}`,
  );

// --- Tasks ---
export const fetchTasks = (projectId: number) =>
  apiFetch<Task[]>(`/api/projects/${projectId}/tasks`);
export const createTask = (projectId: number, data: {
  title: string;
  description?: string | null;
  status?: string;
  priority?: number;
  deadline?: string | null;
}) => apiFetch<Task>(`/api/projects/${projectId}/tasks`, { method: 'POST', body: JSON.stringify(data) });
export const updateTask = (taskId: number, data: Partial<Task>) =>
  apiFetch<Task>(`/api/tasks/${taskId}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteTask = (taskId: number) =>
  apiFetch<void>(`/api/tasks/${taskId}`, { method: 'DELETE' });

// --- Milestones ---
export const fetchMilestones = (projectId: number) =>
  apiFetch<Milestone[]>(`/api/projects/${projectId}/milestones`);
export const createMilestone = (projectId: number, data: {
  name: string;
  deadline?: string | null;
  description?: string | null;
}) => apiFetch<Milestone>(`/api/projects/${projectId}/milestones`, { method: 'POST', body: JSON.stringify(data) });
export const updateMilestone = (milestoneId: number, data: Partial<Milestone>) =>
  apiFetch<Milestone>(`/api/milestones/${milestoneId}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteMilestone = (milestoneId: number) =>
  apiFetch<void>(`/api/milestones/${milestoneId}`, { method: 'DELETE' });

// --- Project Papers ---
export const fetchProjectPapers = (projectId: number) =>
  apiFetch<ProjectPaper[]>(`/api/projects/${projectId}/papers`);
export const linkPaper = (projectId: number, paperId: number) =>
  apiFetch<{ project_id: number; paper_id: number }>(`/api/projects/${projectId}/papers/${paperId}`, { method: 'POST' });
export const unlinkPaper = (projectId: number, paperId: number) =>
  apiFetch<void>(`/api/projects/${projectId}/papers/${paperId}`, { method: 'DELETE' });
export const searchLibrary = (q: string) =>
  apiFetch<Paper[]>(`/api/papers?q=${encodeURIComponent(q)}&limit=20`);
