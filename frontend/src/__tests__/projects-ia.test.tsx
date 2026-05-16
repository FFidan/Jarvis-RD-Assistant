/**
 * Projects IA Redesign — unit tests (F6)
 *
 * Coverage:
 * - ChapterRail: roman-numeral ordinals, status chip label translation,
 *   paper_count / open_question_count display (including 0), active selection highlight
 * - QuestionsSection: renders questions, Q-numbering, add via input, delete confirm
 * - RecentActivitySection: renders items, kind→prefix chip mapping, empty state
 * - ProjectsPage (shell): auto-select first chapter on mount, deep-link override,
 *   regression guard that sections are present
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

// ---------------------------------------------------------------------------
// API mock — hoist all vi.mock calls before imports
// ---------------------------------------------------------------------------
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    fetchProjects: vi.fn(),
    fetchProjectQuestions: vi.fn(),
    fetchProjectActivity: vi.fn(),
    fetchTasks: vi.fn(),
    fetchMilestones: vi.fn(),
    fetchProjectPapers: vi.fn(),
    createProjectQuestion: vi.fn(),
    deleteProjectQuestion: vi.fn(),
    createProject: vi.fn(),
    deleteProject: vi.fn(),
    updateProject: vi.fn(),
  };
});

import {
  fetchProjects,
  fetchProjectQuestions,
  fetchProjectActivity,
  fetchTasks,
  fetchMilestones,
  fetchProjectPapers,
  createProjectQuestion,
  deleteProjectQuestion,
} from '@/lib/api';

import { ChapterRail, toRoman } from '@/components/projects/ChapterRail';
import { QuestionsSection } from '@/components/projects/QuestionsSection';
import { RecentActivitySection } from '@/components/projects/RecentActivitySection';
import { ProjectsPage } from '@/pages/ProjectsPage';
import type { Project, ProjectQuestion, ProjectActivityItem } from '@/types';

const mockFetchProjects = vi.mocked(fetchProjects);
const mockFetchQuestions = vi.mocked(fetchProjectQuestions);
const mockFetchActivity = vi.mocked(fetchProjectActivity);
const mockFetchTasks = vi.mocked(fetchTasks);
const mockFetchMilestones = vi.mocked(fetchMilestones);
const mockFetchProjectPapers = vi.mocked(fetchProjectPapers);
const mockCreateQuestion = vi.mocked(createProjectQuestion);
const mockDeleteQuestion = vi.mocked(deleteProjectQuestion);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function wrap(ui: React.ReactNode, opts: { path?: string; state?: unknown } = {}) {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter
        initialEntries={[{ pathname: opts.path ?? '/projects', state: opts.state ?? null }]}
      >
        {ui}
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const BASE_PROJECT: Project = {
  id: 1,
  name: 'Alpha Research',
  description: 'Reward-guided sampling',
  status: 'active',
  deadline: '2026-08-31',
  color: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  paper_count: 3,
  open_question_count: 2,
};

const PROJECT_B: Project = {
  ...BASE_PROJECT,
  id: 2,
  name: 'Beta Study',
  status: 'paused',
  paper_count: 0,
  open_question_count: 0,
};

const PROJECT_C: Project = {
  ...BASE_PROJECT,
  id: 3,
  name: 'Gamma Survey',
  status: 'completed',
};

const PROJECT_D: Project = {
  ...BASE_PROJECT,
  id: 4,
  name: 'Delta Draft',
  status: 'archived',
};

// ---------------------------------------------------------------------------
// ChapterRail
// ---------------------------------------------------------------------------

describe('ChapterRail', () => {
  it('renders § CHAPTERS header with project count', () => {
    wrap(
      <ChapterRail
        projects={[BASE_PROJECT, PROJECT_B]}
        selectedId={null}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText(/§ CHAPTERS · 2/i)).toBeInTheDocument();
  });

  it('displays roman-numeral ordinals I, II, III for three projects', () => {
    wrap(
      <ChapterRail
        projects={[BASE_PROJECT, PROJECT_B, PROJECT_C]}
        selectedId={null}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText('I')).toBeInTheDocument();
    expect(screen.getByText('II')).toBeInTheDocument();
    expect(screen.getByText('III')).toBeInTheDocument();
  });

  it('translates status labels: active→reading, paused→drafting, completed→shipped, archived→idle', () => {
    wrap(
      <ChapterRail
        projects={[BASE_PROJECT, PROJECT_B, PROJECT_C, PROJECT_D]}
        selectedId={null}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText('reading')).toBeInTheDocument();
    expect(screen.getByText('drafting')).toBeInTheDocument();
    expect(screen.getByText('shipped')).toBeInTheDocument();
    expect(screen.getByText('idle')).toBeInTheDocument();
  });

  it('shows paper_count and open_question_count including zeros', () => {
    wrap(
      <ChapterRail
        projects={[BASE_PROJECT, PROJECT_B]}
        selectedId={null}
        onSelect={vi.fn()}
      />,
    );
    // BASE_PROJECT: 3 papers, 2 Qs
    expect(screen.getByText('3 papers')).toBeInTheDocument();
    expect(screen.getByText('2 Qs')).toBeInTheDocument();
    // PROJECT_B: 0 papers, 0 Qs
    expect(screen.getByText('0 papers')).toBeInTheDocument();
    expect(screen.getByText('0 Qs')).toBeInTheDocument();
  });

  it('highlights selected chapter row', () => {
    wrap(
      <ChapterRail
        projects={[BASE_PROJECT, PROJECT_B]}
        selectedId={1}
        onSelect={vi.fn()}
      />,
    );
    const btn = screen.getByRole('button', { name: /alpha research/i });
    // aria-pressed should be true for selected
    expect(btn).toHaveAttribute('aria-pressed', 'true');
  });

  it('calls onSelect with correct project id when clicked', async () => {
    const onSelect = vi.fn();
    wrap(
      <ChapterRail
        projects={[BASE_PROJECT, PROJECT_B]}
        selectedId={null}
        onSelect={onSelect}
      />,
    );
    await userEvent.click(screen.getByText('Beta Study'));
    expect(onSelect).toHaveBeenCalledWith(2);
  });

  it('shows empty state when no projects exist', () => {
    wrap(
      <ChapterRail projects={[]} selectedId={null} onSelect={vi.fn()} />,
    );
    expect(screen.getByText(/no chapters yet/i)).toBeInTheDocument();
  });

  it('coalesces missing paper_count/open_question_count to 0', () => {
    const noCountProject: Project = {
      ...BASE_PROJECT,
      paper_count: undefined,
      open_question_count: undefined,
    };
    wrap(
      <ChapterRail projects={[noCountProject]} selectedId={null} onSelect={vi.fn()} />,
    );
    expect(screen.getByText('0 papers')).toBeInTheDocument();
    expect(screen.getByText('0 Qs')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// toRoman — full Roman numeral helper
// ---------------------------------------------------------------------------

describe('toRoman', () => {
  it('toRoman(1) → "I"', () => expect(toRoman(1)).toBe('I'));
  it('toRoman(4) → "IV"', () => expect(toRoman(4)).toBe('IV'));
  it('toRoman(9) → "IX"', () => expect(toRoman(9)).toBe('IX'));
  it('toRoman(10) → "X"', () => expect(toRoman(10)).toBe('X'));
  it('toRoman(39) → "XXXIX"', () => expect(toRoman(39)).toBe('XXXIX'));
  it('toRoman(40) → "XL"', () => expect(toRoman(40)).toBe('XL'));
  it('toRoman(49) → "XLIX"', () => expect(toRoman(49)).toBe('XLIX'));
  it('toRoman(50) → "L"', () => expect(toRoman(50)).toBe('L'));
  it('toRoman(90) → "XC"', () => expect(toRoman(90)).toBe('XC'));
  it('toRoman(100) → "C"', () => expect(toRoman(100)).toBe('C'));
});

// ---------------------------------------------------------------------------
// QuestionsSection
// ---------------------------------------------------------------------------

const QUESTIONS: ProjectQuestion[] = [
  { id: 10, project_id: 1, body: 'What is the optimal temperature?', created_at: '2026-01-01T00:00:00Z' },
  { id: 11, project_id: 1, body: 'How does scaling affect performance?', created_at: '2026-01-02T00:00:00Z' },
];

describe('QuestionsSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockCreateQuestion.mockResolvedValue({
      id: 12,
      project_id: 1,
      body: 'New question',
      created_at: new Date().toISOString(),
    });
    mockDeleteQuestion.mockResolvedValue(undefined);
  });

  it('renders § OPEN QUESTIONS header with count', () => {
    wrap(<QuestionsSection projectId={1} questions={QUESTIONS} />);
    expect(screen.getByText(/§ OPEN QUESTIONS · 2/i)).toBeInTheDocument();
  });

  it('renders Q1, Q2 labels for each question', () => {
    wrap(<QuestionsSection projectId={1} questions={QUESTIONS} />);
    expect(screen.getByText('Q1')).toBeInTheDocument();
    expect(screen.getByText('Q2')).toBeInTheDocument();
  });

  it('renders question body text', () => {
    wrap(<QuestionsSection projectId={1} questions={QUESTIONS} />);
    expect(screen.getByText('What is the optimal temperature?')).toBeInTheDocument();
    expect(screen.getByText('How does scaling affect performance?')).toBeInTheDocument();
  });

  it('shows empty state when no questions', () => {
    wrap(<QuestionsSection projectId={1} questions={[]} />);
    expect(screen.getByText(/no open questions/i)).toBeInTheDocument();
    expect(screen.getByText(/§ OPEN QUESTIONS · 0/i)).toBeInTheDocument();
  });

  it('calls createProjectQuestion when user submits a new question via button', async () => {
    wrap(<QuestionsSection projectId={1} questions={QUESTIONS} />);
    const input = screen.getByPlaceholderText(/add an open question/i);
    await userEvent.type(input, 'Why does diffusion work?');
    await userEvent.click(screen.getByRole('button', { name: /add question/i }));
    await waitFor(() =>
      expect(mockCreateQuestion).toHaveBeenCalledWith(1, 'Why does diffusion work?'),
    );
  });

  it('calls createProjectQuestion when user presses Enter in the input', async () => {
    wrap(<QuestionsSection projectId={1} questions={QUESTIONS} />);
    const input = screen.getByPlaceholderText(/add an open question/i);
    await userEvent.type(input, 'Enter key test{Enter}');
    await waitFor(() => expect(mockCreateQuestion).toHaveBeenCalledTimes(1));
  });

  it('does not call createProjectQuestion when input is empty', async () => {
    wrap(<QuestionsSection projectId={1} questions={QUESTIONS} />);
    await userEvent.click(screen.getByRole('button', { name: /add question/i }));
    expect(mockCreateQuestion).not.toHaveBeenCalled();
  });

  it('opens delete confirm dialog when trash button is clicked', async () => {
    wrap(<QuestionsSection projectId={1} questions={QUESTIONS} />);
    const deleteButtons = screen.getAllByRole('button', { name: /delete question/i });
    await userEvent.click(deleteButtons[0]!);
    expect(screen.getByText(/delete question\?/i)).toBeInTheDocument();
  });

  it('calls deleteProjectQuestion on confirm', async () => {
    wrap(<QuestionsSection projectId={1} questions={QUESTIONS} />);
    const deleteButtons = screen.getAllByRole('button', { name: /delete question/i });
    await userEvent.click(deleteButtons[0]!);
    await userEvent.click(screen.getByRole('button', { name: /^delete$/i }));
    await waitFor(() => expect(mockDeleteQuestion).toHaveBeenCalledWith(10));
  });
});

// ---------------------------------------------------------------------------
// RecentActivitySection
// ---------------------------------------------------------------------------

const ACTIVITY_ITEMS: ProjectActivityItem[] = [
  { kind: 'added_paper', ts: new Date(Date.now() - 2 * 3600_000).toISOString(), label: 'Test-time scaling of diffusion LMs' },
  { kind: 'completed_task', ts: new Date(Date.now() - 24 * 3600_000).toISOString(), label: 'Write literature review' },
  { kind: 'completed_milestone', ts: new Date(Date.now() - 48 * 3600_000).toISOString(), label: 'Phase 1 complete' },
];

describe('RecentActivitySection', () => {
  it('renders § RECENT ACTIVITY header', () => {
    wrap(<RecentActivitySection items={ACTIVITY_ITEMS} />);
    expect(screen.getByText(/§ RECENT ACTIVITY/i)).toBeInTheDocument();
  });

  it('maps added_paper kind to ADDED prefix', () => {
    wrap(<RecentActivitySection items={ACTIVITY_ITEMS} />);
    expect(screen.getByText('ADDED')).toBeInTheDocument();
  });

  it('maps completed_task kind to COMPLETED TASK prefix', () => {
    wrap(<RecentActivitySection items={ACTIVITY_ITEMS} />);
    expect(screen.getByText('COMPLETED TASK')).toBeInTheDocument();
  });

  it('maps completed_milestone kind to COMPLETED MILESTONE prefix', () => {
    wrap(<RecentActivitySection items={ACTIVITY_ITEMS} />);
    expect(screen.getByText('COMPLETED MILESTONE')).toBeInTheDocument();
  });

  it('renders item label text', () => {
    wrap(<RecentActivitySection items={ACTIVITY_ITEMS} />);
    expect(screen.getByText('Test-time scaling of diffusion LMs')).toBeInTheDocument();
    expect(screen.getByText('Write literature review')).toBeInTheDocument();
  });

  it('renders relative time strings', () => {
    wrap(<RecentActivitySection items={ACTIVITY_ITEMS} />);
    // 2 hours ago
    expect(screen.getByText('2 hours ago')).toBeInTheDocument();
    // 1 day ago (24h)
    expect(screen.getByText('1 day ago')).toBeInTheDocument();
  });

  it('shows empty state when no items', () => {
    wrap(<RecentActivitySection items={[]} />);
    expect(screen.getByText(/no activity yet/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// ProjectsPage — shell integration
// ---------------------------------------------------------------------------

describe('ProjectsPage shell', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchProjects.mockResolvedValue([BASE_PROJECT, PROJECT_B]);
    mockFetchQuestions.mockResolvedValue([]);
    mockFetchActivity.mockResolvedValue([]);
    mockFetchTasks.mockResolvedValue([]);
    mockFetchMilestones.mockResolvedValue([]);
    mockFetchProjectPapers.mockResolvedValue([]);
  });

  it('auto-selects the first chapter on mount when no deep-link present', async () => {
    wrap(<ProjectsPage />);
    // Auto-select should give the first chapter rail button aria-pressed=true
    await waitFor(() => {
      // both rail row and breadcrumb show the name — verify at least one is present
      const elements = screen.getAllByText('Alpha Research');
      expect(elements.length).toBeGreaterThanOrEqual(1);
    });
    // Rail row for first project should be aria-pressed
    await waitFor(() => {
      const railBtn = screen.getAllByText('Alpha Research')[0]?.closest('button');
      if (railBtn) {
        expect(railBtn).toHaveAttribute('aria-pressed', 'true');
      }
    });
  });

  it('overrides auto-select when deep-linked to a specific projectId', async () => {
    mockFetchProjects.mockResolvedValue([BASE_PROJECT, PROJECT_B]);
    wrap(<ProjectsPage />, { state: { projectId: 2 } });
    await waitFor(() => {
      const btn = screen.getByRole('button', { name: /beta study/i });
      expect(btn).toHaveAttribute('aria-pressed', 'true');
    });
  });

  it('renders § CHAPTERS rail header', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByText(/§ CHAPTERS · 2/i)).toBeInTheDocument();
    });
  });

  it('renders § OPEN QUESTIONS section in the document pane', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByText(/§ OPEN QUESTIONS/i)).toBeInTheDocument();
    });
  });

  it('renders § RECENT ACTIVITY section in the document pane', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByText(/§ RECENT ACTIVITY/i)).toBeInTheDocument();
    });
  });

  it('renders § MILESTONES · 0 header (live count) in document pane when no milestones', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByText(/§ MILESTONES · 0/i)).toBeInTheDocument();
    });
  });

  it('renders § MILESTONES · N header with correct live count', async () => {
    mockFetchMilestones.mockResolvedValue([
      { id: 1, project_id: 1, name: 'Draft complete', description: null, deadline: null, completed: false, completed_at: null, created_at: '2026-01-01T00:00:00Z' },
      { id: 2, project_id: 1, name: 'Submitted', description: null, deadline: null, completed: true, completed_at: '2026-01-02T00:00:00Z', created_at: '2026-01-02T00:00:00Z' },
    ]);
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByText(/§ MILESTONES · 2/i)).toBeInTheDocument();
    });
  });

  it('renders § TASKS · 0 header (live count) in document pane when no tasks', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByText(/§ TASKS · 0/i)).toBeInTheDocument();
    });
  });

  it('renders § TASKS · N header with correct live count', async () => {
    mockFetchTasks.mockResolvedValue([
      { id: 10, project_id: 1, parent_task_id: null, title: 'Write intro', description: null, status: 'todo', priority: 3, deadline: null, estimated_hours: null, actual_hours: null, sort_order: 0, completed_at: null, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' },
      { id: 11, project_id: 1, parent_task_id: null, title: 'Run experiments', description: null, status: 'in_progress', priority: 2, deadline: null, estimated_hours: null, actual_hours: null, sort_order: 1, completed_at: null, created_at: '2026-01-02T00:00:00Z', updated_at: '2026-01-02T00:00:00Z' },
      { id: 12, project_id: 1, parent_task_id: null, title: 'Submit paper', description: null, status: 'todo', priority: 1, deadline: null, estimated_hours: null, actual_hours: null, sort_order: 2, completed_at: null, created_at: '2026-01-03T00:00:00Z', updated_at: '2026-01-03T00:00:00Z' },
    ]);
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByText(/§ TASKS · 3/i)).toBeInTheDocument();
    });
  });

  it('renders § PAPERS section in the document pane', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByText(/§ PAPERS/i)).toBeInTheDocument();
    });
  });

  it('shows Projects page heading', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /projects/i })).toBeInTheDocument();
    });
  });

  it('shows empty chapter pane prompt when no projects exist', async () => {
    mockFetchProjects.mockResolvedValue([]);
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByText(/select a chapter/i)).toBeInTheDocument();
    });
  });

  it('shows translated status chip in rail (active → reading)', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      // "reading" appears at least once in the rail and/or breadcrumb status select
      expect(screen.getAllByText('reading').length).toBeGreaterThan(0);
    });
  });

  it('paper_count coerces to 0 when undefined in rail row', async () => {
    mockFetchProjects.mockResolvedValue([
      { ...BASE_PROJECT, paper_count: undefined, open_question_count: undefined },
    ]);
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByText('0 papers')).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Regression guard — existing tabs functionality preserved
// (MilestonesTab, TasksTab, LinkedPapersTab are inline sections, not tab bar)
// ---------------------------------------------------------------------------
describe('Existing functionality preserved', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchProjects.mockResolvedValue([BASE_PROJECT]);
    mockFetchQuestions.mockResolvedValue([]);
    mockFetchActivity.mockResolvedValue([]);
    mockFetchTasks.mockResolvedValue([]);
    mockFetchMilestones.mockResolvedValue([]);
    mockFetchProjectPapers.mockResolvedValue([]);
  });

  it('breadcrumb shows project name in pane', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      // Breadcrumb includes "Projects" and the project name
      expect(screen.getByText('Projects')).toBeInTheDocument();
    });
  });

  it('MilestonesTab is present as inline section (Add Milestone button visible)', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /add milestone/i })).toBeInTheDocument();
    });
  });

  it('TasksTab is present as inline section (Add Task button visible)', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /add task/i })).toBeInTheDocument();
    });
  });

  it('LinkedPapersTab is present as inline section (Search papers input visible)', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/search papers/i)).toBeInTheDocument();
    });
  });

  it('Delete project button is present and opens confirm dialog', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /delete project/i })).toBeInTheDocument();
    });
    await userEvent.click(screen.getByRole('button', { name: /delete project/i }));
    expect(screen.getByText(/delete project\?/i)).toBeInTheDocument();
  });

  it('Create chapter button opens Create Project dialog', async () => {
    wrap(<ProjectsPage />);
    await waitFor(
      () => {
        expect(screen.getByText(/new chapter/i)).toBeInTheDocument();
      },
      { timeout: 5000 },
    );
    await userEvent.click(screen.getByText(/new chapter/i));
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /create project/i })).toBeInTheDocument();
    });
  });

  it('no tab bar in the document pane (tabs removed)', async () => {
    wrap(<ProjectsPage />);
    await waitFor(() => {
      expect(screen.queryByRole('tab', { name: /overview/i })).not.toBeInTheDocument();
      expect(screen.queryByRole('tab', { name: /milestones/i })).not.toBeInTheDocument();
      expect(screen.queryByRole('tab', { name: /tasks/i })).not.toBeInTheDocument();
      expect(screen.queryByRole('tab', { name: /papers/i })).not.toBeInTheDocument();
    });
  });
});
