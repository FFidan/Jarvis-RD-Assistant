// Projects, open questions, recent activity, tasks, milestones, and the
// project↔paper link surface.
import { apiFetchJson, apiFetchVoid } from './core';
import {
  librarySearchPaperSchema,
  milestoneSchema,
  projectActivitySchema,
  projectPaperLinkSchema,
  projectPaperSchema,
  projectQuestionSchema,
  projectSchema,
  taskSchema,
} from './schemas/projects';
import type { LibrarySearchPaper } from './schemas/projects';
import type {
  Project,
  Task,
  Milestone,
  ProjectPaper,
  ProjectQuestion,
  ProjectActivityItem,
} from '@/types';

// --- Projects ---
export const fetchProjects = (status?: string): Promise<Project[]> =>
  apiFetchJson(`/api/projects${status ? `?status=${status}` : ''}`, projectSchema.array());
export const createProject = (data: {
  name: string;
  description?: string | null;
  status?: string;
  deadline?: string | null;
}): Promise<Project> => apiFetchJson('/api/projects', projectSchema, { method: 'POST', body: JSON.stringify(data) });
export const updateProject = (id: number, data: Partial<Project>): Promise<Project> =>
  apiFetchJson(`/api/projects/${id}`, projectSchema, { method: 'PUT', body: JSON.stringify(data) });
export const deleteProject = (id: number) =>
  apiFetchVoid(`/api/projects/${id}`, { method: 'DELETE' });

// --- Project Open Questions (Projects § OPEN QUESTIONS) ---
export const fetchProjectQuestions = (projectId: number): Promise<ProjectQuestion[]> =>
  apiFetchJson(`/api/projects/${projectId}/questions`, projectQuestionSchema.array());
export const createProjectQuestion = (projectId: number, body: string): Promise<ProjectQuestion> =>
  apiFetchJson(`/api/projects/${projectId}/questions`, projectQuestionSchema, {
    method: 'POST',
    body: JSON.stringify({ body }),
  });
/** DELETE is addressed by question id (own /api/questions prefix). */
export const deleteProjectQuestion = (questionId: number) =>
  apiFetchVoid(`/api/questions/${questionId}`, { method: 'DELETE' });

// --- Project Recent Activity (Projects § RECENT ACTIVITY) ---
export const fetchProjectActivity = (projectId: number, limit?: number): Promise<ProjectActivityItem[]> =>
  apiFetchJson(
    `/api/projects/${projectId}/activity${limit ? `?limit=${limit}` : ''}`,
    projectActivitySchema.array(),
  );

// --- Tasks ---
export const fetchTasks = (projectId: number): Promise<Task[]> =>
  apiFetchJson(`/api/projects/${projectId}/tasks`, taskSchema.array());
export const createTask = (projectId: number, data: {
  title: string;
  description?: string | null;
  status?: string;
  priority?: number;
  deadline?: string | null;
}): Promise<Task> => apiFetchJson(`/api/projects/${projectId}/tasks`, taskSchema, { method: 'POST', body: JSON.stringify(data) });
export const updateTask = (taskId: number, data: Partial<Task>): Promise<Task> =>
  apiFetchJson(`/api/tasks/${taskId}`, taskSchema, { method: 'PUT', body: JSON.stringify(data) });
export const deleteTask = (taskId: number) =>
  apiFetchVoid(`/api/tasks/${taskId}`, { method: 'DELETE' });

// --- Milestones ---
export const fetchMilestones = (projectId: number): Promise<Milestone[]> =>
  apiFetchJson(`/api/projects/${projectId}/milestones`, milestoneSchema.array());
export const createMilestone = (projectId: number, data: {
  name: string;
  deadline?: string | null;
  description?: string | null;
}): Promise<Milestone> => apiFetchJson(`/api/projects/${projectId}/milestones`, milestoneSchema, { method: 'POST', body: JSON.stringify(data) });
export const updateMilestone = (milestoneId: number, data: Partial<Milestone>): Promise<Milestone> =>
  apiFetchJson(`/api/milestones/${milestoneId}`, milestoneSchema, { method: 'PUT', body: JSON.stringify(data) });
export const deleteMilestone = (milestoneId: number) =>
  apiFetchVoid(`/api/milestones/${milestoneId}`, { method: 'DELETE' });

// --- Project Papers ---
export const fetchProjectPapers = (projectId: number): Promise<ProjectPaper[]> =>
  apiFetchJson(`/api/projects/${projectId}/papers`, projectPaperSchema.array());
export const linkPaper = (projectId: number, paperId: number): Promise<{ project_id: number; paper_id: number }> =>
  apiFetchJson(`/api/projects/${projectId}/papers/${paperId}`, projectPaperLinkSchema, { method: 'POST' });
export const unlinkPaper = (projectId: number, paperId: number) =>
  apiFetchVoid(`/api/projects/${projectId}/papers/${paperId}`, { method: 'DELETE' });
export const searchLibrary = (q: string): Promise<LibrarySearchPaper[]> =>
  apiFetchJson(`/api/papers?q=${encodeURIComponent(q)}&limit=20`, librarySearchPaperSchema.array());
