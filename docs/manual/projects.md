<!-- verified-against-UI: 2026-05-18 | routes: /projects -->

# Projects

The **Projects** page at `/projects` lets you group related papers, tasks, and notes around a specific research question or deliverable.

<!-- screenshot: projects -->

---

## Layout

The Projects page uses a **two-pane layout**:

- **ChapterRail** (left) — a scrollable list of your projects. Click any project to open it in the right pane.
- **ChapterPane → ProjectDetail** (right) — the detail view for the selected project, with tabs for different aspects of the project.

---

## ProjectDetail tabs

### Overview

The Overview tab shows:

- **Description** — the project's name and description as entered when it was created.
- **RecentActivity** — a chronological log of recent actions taken in this project: papers linked, tasks created or completed, notes updated.
- **Questions** — a list of research questions associated with this project, which can be added and edited in the tab.

### Tasks

The Tasks tab is a simple task board for the project:

- **Create** a task by typing in the input field and pressing Enter.
- **Complete** a task using the checkbox next to it. Completed tasks move to a done list.
- **Delete** a task using the remove button.

Tasks are scoped to the project and are not shared with other projects.

### Milestones

The Milestones tab lets you define named milestones for the project with optional target dates. Use milestones to track progress towards deliverables such as a paper submission or a reading sprint.

### LinkedPapers

The LinkedPapers tab lists all papers attached to this project. Papers can be linked to the project from this tab or from the [Paper Detail](paper-detail.md) page. A paper can belong to multiple projects simultaneously.

---

## Deep-link from My Day

The **ProjectsSection** on the [My Day](home-my-day.md) page shows your active projects and links directly to their detail view here at `/projects`. This makes it easy to continue work on a project from your daily workspace without having to navigate manually.

---

## Related pages

- [My Day & Home](home-my-day.md) — ProjectsSection links to your active projects.
- [Paper Detail](paper-detail.md) — link papers to projects from the UserStateForm in the right rail.
- [Analytics](analytics.md) — activity charts reflect task and paper activity across all projects.
