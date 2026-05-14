# Data Contracts — backend changes for v5

This file lists every new entity, every new API field, and the migration steps. Backwards-compatible — existing endpoints keep working.

---

## New entities

### `thread`
A half-finished reading or derivation. Distinct from `task` because it has a section anchor and progress, and from `paper` because it tracks engagement state.

```ts
interface Thread {
  id: number;
  user_id: number;
  paper_id: number | null;          // null for pure derivations / notes not tied to a paper
  title: string;                    // "Reading: Latent ODEs for irregular time series (Rubanova 2019)"
  anchor: string;                   // "§4.1 ELBO derivation" — section/page user is stuck at
  progress: number;                 // 0.0 — 1.0; user-set or inferred from time-spent
  last_touched_at: Timestamp;
  created_at: Timestamp;
  updated_at: Timestamp;
}
```

**Migration:**
```sql
CREATE TABLE threads (
  id           BIGSERIAL PRIMARY KEY,
  user_id      BIGINT NOT NULL REFERENCES users(id),
  paper_id     BIGINT REFERENCES papers(id),
  title        TEXT NOT NULL,
  anchor       TEXT,
  progress     REAL DEFAULT 0.0,
  last_touched_at TIMESTAMPTZ,
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  updated_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX threads_user_recent_idx ON threads(user_id, last_touched_at DESC);
```

**Endpoints:**
- `GET /threads/recent?limit=3` — top N by `last_touched_at`
- `POST /threads` — create
- `PATCH /threads/:id` — update progress / anchor / last_touched_at
- `POST /threads/:id/touch` — bump last_touched_at (called when the user opens a paper that has a thread)

---

### `journal_entry`
The end-of-day reflection. One per user per date.

```ts
interface JournalEntry {
  id: number;
  user_id: number;
  date: Date;                       // YYYY-MM-DD, unique per user
  prompts: {
    one_thing_that_worked: string;
    blocker: string;
    tomorrow_first: string;
  };
  sealed_at: Timestamp | null;      // when the user submitted ⇧↩
  created_at: Timestamp;
  updated_at: Timestamp;
}
```

**Migration:**
```sql
CREATE TABLE journal_entries (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT NOT NULL REFERENCES users(id),
  date        DATE NOT NULL,
  prompts     JSONB NOT NULL DEFAULT '{}'::jsonb,
  sealed_at   TIMESTAMPTZ,
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  updated_at  TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(user_id, date)
);
```

**Endpoints:**
- `GET /journal/today` — returns today's entry (creates an empty one if absent)
- `PATCH /journal/today` — autosave on input blur
- `POST /journal/today/seal` — sets `sealed_at = NOW()` when the user submits ⇧↩

---

## Existing entities — added fields

### `task`
Add a `color` field to support the project-badge color coding. Inherit from `project.color` if not explicitly set.

```diff
  interface Task {
    id: number;
    title: string;
    project: string | null;
+   color: string | null;     // hex; defaults to project.color or zinc-500
    priority: "low" | "med" | "high";
    completed_at: Timestamp | null;
    // ...
  }
```

Migration:
```sql
ALTER TABLE tasks ADD COLUMN color VARCHAR(9);
```

### `project`
Add `color`, `milestone`, `due` fields if not already present.

```diff
  interface Project {
    id: number;
    name: string;
+   color: string;            // hex
    progress: number;         // 0–100
+   next_milestone: string;   // free-text label
+   next_milestone_due: Date;
    status: "on-track" | "at-risk" | "blocked";
    // ...
  }
```

---

## Endpoint changes — `/my-day`

Existing endpoint adds 3 fields. **Don't break existing consumers.**

```ts
interface MyDayResponse {
  // ─── existing ────────────────────────────────────────
  user: { name: string; focus_today: number; focus_streak: number };
  pulse: { generated_at: Timestamp; cards: PulseCard[] };
  action_items: ActionItem[];
  missing_foundational: MissingFoundational[];
  tasks: Task[];
  cards: { due: number; learning: number; retention_30d: number; streak: number; reviewed_today: number };
  projects: Project[];
  services: ServiceHealth;

  // ─── NEW ─────────────────────────────────────────────
  yesterday: {
    focused: number;            // hours
    cards_reviewed: number;
    completed: string[];        // top 2-3 completed task titles
    deferred: string[];         // tasks marked carry-over
  };
  threads: Thread[];            // top 3 by last_touched_at
  completed_today: { id: number; title: string; at: string }[];   // tasks completed today
}
```

---

## New endpoint — `POST /papers/process_batch`

Powers the "Process all (N)" button on the Triage row.

```ts
// Request
{ paper_ids: number[] }

// Response
{ jobs: { paper_id: number; job_id: string }[] }
```

Each entry kicks off the existing `paper.process` job. The frontend polls existing jobs status to update UI.

---

## New endpoint — `POST /pomodoro/start`

If not already present (`pomodoroStartWork({task})` in the existing code suggests it is — verify), make sure it accepts a `task_id` so the topbar timer can show the active task title.

```ts
// Request
{ task_id: number; duration_min?: number }   // default 25

// Response
{ session_id: string; ends_at: Timestamp; task: Task }
```

---

## Hero default algorithm — server hint

To avoid client-side complexity, return a `hero_default` hint in `/my-day`:

```ts
{
  // ...existing fields...
  hero_default: {
    mode: "pulse" | "thread" | "task";
    reason: string;             // human-readable, optional
  };
}
```

Server logic (pseudocode):
```ts
if (interruptedPomodoroFromYesterday) return { mode: "task", reason: "23:48 in last session" };
if (anyThread.progress > 0.7 && hoursSince(lastTouchedAt) < 24) return { mode: "thread", reason: "..." };
return { mode: "pulse", reason: "today's top pick" };
```

The client may override with the cached `localStorage('myday.heroMode')` if the user has explicitly switched modes during the session.

---

## Telemetry events to emit

Add these so we can measure adoption:

| Event | When |
|---|---|
| `myday.hero.mode_changed` | User clicks a different mode in the picker |
| `myday.hero.action.{open,accept,skip,save,resume}` | Hero CTA tapped |
| `myday.task.focus_started` | ▶ focus on a task → Pomodoro |
| `myday.thread.resumed` | Click on a thread row |
| `myday.eod.sealed` | EOD reflection submitted |
| `myday.section.jumped` | `j`/`k` keyboard nav |
