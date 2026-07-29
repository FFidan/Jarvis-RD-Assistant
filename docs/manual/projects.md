<!-- verified-against-UI: 2026-07-25 | routes: /projects -->

# Projects

The **Projects** page at `/projects` lets you group related papers, tasks, and research notes around a specific question or deliverable. Each project is called a **chapter**.

<!-- screenshot: projects -->

---

## Layout

The Projects page uses a two-pane layout:

- **ChapterRail** (left) — a scrollable list of all your chapters. Each row shows the chapter number (in Roman numerals), name, status chip, and a count of linked papers and open questions. Click any chapter to open it. A **New Chapter** button at the bottom of the rail opens a create dialog where you enter a name and optional description.
- **ChapterPane** (right) — a scrollable document view for the selected chapter. If no chapter is selected, a placeholder prompts you to choose one from the rail.

---

## Chapter statuses

The status chip on each chapter can be changed inline from the ChapterPane header.

| Status (display) | Meaning |
|-----------------|---------|
| **In progress** | Actively working on this chapter |
| **Draft** | Paused; work in progress |
| **Completed** | Completed |
| **Archived** | Dormant; no active work |

---

## ChapterPane — the chapter document

Selecting a chapter opens its **ChapterPane**, a single scrollable document divided into labelled sections:

### Header

The chapter name, an italic subtitle combining the description and deadline (if set), and a status chip that opens an inline status selector. A pencil button next to the deadline opens a date picker; "Clear deadline" removes it. A trash icon in the header triggers a delete confirmation for the chapter.

### § Open Questions

A list of research questions associated with this chapter. Add new questions and mark existing ones resolved directly in this section.

### § Recent Activity

A chronological log of recent actions in this chapter: papers linked, tasks created or completed, questions added.

### § Milestones

Named milestones with optional target dates. Use milestones to track progress toward deliverables such as a paper submission or a reading sprint. Add, edit, and delete milestones inline.

### § Tasks

A simple task list. Create tasks by entering text, complete them with the checkbox, and delete them with the remove button. Tasks are scoped to this chapter and not shared with other chapters.

### § Papers

All papers linked to this chapter. Link and unlink papers from this section. A paper can belong to multiple chapters simultaneously.

---

## Deep-link from My Day

The **ProjectsSection** on the [My Day](home-my-day.md) page shows your active chapters and links directly to their detail view here at `/projects`. This makes it easy to continue chapter work from your daily workspace without navigating manually.

---

## Related pages

- [My Day & Home](home-my-day.md) — ProjectsSection links to your active chapters.
- [Analytics](analytics.md) — activity charts reflect task and paper activity across all chapters.
