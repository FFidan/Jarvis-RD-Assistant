# Paper Lifecycle Redesign — Design Spec
**Status:** DRAFT (awaiting user review)
**Date:** 2026-04-29
**Supersedes:** [paper-lifecycle-contract.md](paper-lifecycle-contract.md), [feed-information-architecture.md](feed-information-architecture.md)
**Author:** Brainstormed with Claude (Opus 4.7) + audit findings

---

## 0. Why This Spec Exists

The Sprint-7 / WS-AH lifecycle (saved/dismissed/starred/archived booleans + status enum + preference) shipped successfully but exposed structural problems during browser-driven audit and review:

1. **5 booleans + 1 enum = 96 theoretically-valid combinations**, the majority of which are nonsense states that the contract pins by convention but the schema cannot enforce.
2. **`preference='down'` is a write-only column.** The recommender does not consult it. The "this paper was a bad fit" signal flows only as a soft LLM hint via `pulse_ratings` — no deterministic learning loop.
3. **Star / Save / Bookmark trio is asymmetric and overloaded.** Five UI surfaces can set `starred=TRUE`, `/bookmark` auto-saves on star but doesn't auto-unsave on unstar, and PRD treats "bookmark" and "star" as synonyms.
4. **Mark Read vs Archive overlap.** Both express "I'm done with this paper" but live on different axes.
5. **Feedback buttons appear on Pulse cards but the loop is broken** — clicking 👎 today only seeds an LLM prompt with paper titles; the cosine recommender ignores the signal entirely.
6. **No source/origin awareness.** Feedback buttons on user-uploaded or manually-searched papers are noise (no recommendation to grade), but the UI cannot distinguish today.

Because the product is **pre-launch**, we have a one-time opportunity to collapse the schema to a clean shape before users depend on the public API surface. This spec describes that redesign.

---

## 1. The Three Concerns (Separated)

A paper-user relationship has exactly **three orthogonal concerns**. The redesign maps each concern to its own column / table:

| Concern | Question it answers | Storage |
|---|---|---|
| **Lifecycle** | Where am I in my workflow with this paper? | `paper_user_state.state` (single ENUM) |
| **Curation** | Is this a favourite? | `paper_user_state.starred` (single BOOLEAN) |
| **Recommendation feedback** | Should the LLM/recommender learn from this? | `recommendation_feedback` (separate TABLE) |

These three concerns must NOT bleed into each other in code. Specifically:
- A lifecycle action (`Trash`) does NOT write feedback. (Combined `🗑+👎` writes both, explicitly.)
- A curation action (`Star`) does NOT change lifecycle.
- A feedback action (`👍` / `👎`) does NOT change lifecycle.

The combined `🗑+👎` button is a one-click convenience — internally it issues both writes via a transactional API call, but the data layer treats them as independent.

---

## 2. Lifecycle State Machine

### 2.1 States

| State | Meaning | Default surface |
|---|---|---|
| `inbox` | System-fetched or freshly-arrived; user has not triaged. | Inbox |
| `to_read` | User said "yes, save this for later." The reading list. | Library / Reading List |
| `reading` | Currently engaging with this paper. | Library / Reading |
| `done` | Finished — searchable forever, out of active surfaces. | Library / Done (sub-filter) |
| `trash` | Rejected. Excluded from recommender, Pulse candidate pool, KG. Recoverable via Restore. | Trash |

### 2.2 Transitions

```
[inbox] ─Save────────► [to_read] ─Mark Reading──► [reading] ─Mark Done──► [done]
   │                       │                          │                      │
   │                       │                          │                      │
   ├─Skip───────────────────────────────────────────────────────────────────►│
   │                                                                         │
   ├─Trash─────────────────┬──────────────────────────┬──────────────────────┤
   │                       │                          │                      │
   ▼                       ▼                          ▼                      ▼
[trash]  ◄──Restore (returns to state_before_trash)─────────────────────────┘
   │
   └─Hard Delete──► [gone] (row deleted; Qdrant points purged; FKs cascade per init.sql)
```

**Back-edges (also legal):**
- `reading → to_read` (set aside without finishing)
- `done → to_read` (re-open / want to re-read)

**Auto-transition:** opening Paper Detail of a `to_read` paper transitions to `reading`. Opening from `inbox` does NOT auto-save (preserves user intent — skim before committing).

### 2.3 Restore semantics

- `state_before_trash` is set on EVERY trash transition (overwrites prior value if any).
- `Restore` reads `state_before_trash`, sets `state = state_before_trash`, clears `state_before_trash`.
- A paper trashed directly from `inbox` restores to `inbox`. A paper trashed from `done` restores to `done`. Etc.

### 2.4 Star semantics

- `starred` is orthogonal to `state`. Toggle-able in any state.
- Visible UI rule: starred sub-filter excludes `trash` (hide starred trash from default view; user can find via `state='trash'`).
- Read by:
  - Pulse `library_centroid` (positive embedding signal, see §7)
  - Recommender seed selection
  - Settings UI ("starred count" stat)

---

## 3. Schema (Migrations 047 + 048 + 049)

### 3.1 Migration 047 — collapse `paper_user_state` lifecycle

```sql
BEGIN;

-- Add new state column
ALTER TABLE paper_user_state
    ADD COLUMN state TEXT NOT NULL DEFAULT 'inbox'
        CHECK (state IN ('inbox','to_read','reading','done','trash')),
    ADD COLUMN state_before_trash TEXT
        CHECK (state_before_trash IN ('inbox','to_read','reading','done')
               OR state_before_trash IS NULL);

-- Backfill from existing booleans (deterministic mapping)
UPDATE paper_user_state SET state = CASE
    WHEN dismissed = TRUE                           THEN 'trash'
    WHEN archived = TRUE OR status = 'read'         THEN 'done'
    WHEN status = 'reading'                         THEN 'reading'
    WHEN saved = TRUE                               THEN 'to_read'
    ELSE 'inbox'
END;

-- For trash rows, set state_before_trash so Restore works
UPDATE paper_user_state SET state_before_trash = CASE
    WHEN archived = TRUE OR status = 'read'         THEN 'done'
    WHEN status = 'reading'                         THEN 'reading'
    WHEN saved = TRUE                               THEN 'to_read'
    ELSE 'inbox'
END
WHERE state = 'trash';

-- Drop legacy columns
ALTER TABLE paper_user_state
    DROP COLUMN saved,
    DROP COLUMN dismissed,
    DROP COLUMN archived,
    DROP COLUMN status,
    DROP COLUMN preference;

-- updated_at trigger remains (from migration 046)
COMMIT;
```

**Rollback strategy:** the migration is *not* trivially reversible because the boolean→state mapping loses the `(saved=TRUE, archived=TRUE, status='reading')` combination distinct from `(saved=TRUE, archived=TRUE, status='read')`. Both map to `done`. This is acceptable because such combinations are incoherent under the current contract anyway.

### 3.2 Migration 048 — `papers.discovery_origin`

```sql
BEGIN;

ALTER TABLE papers
    ADD COLUMN discovery_origin TEXT NOT NULL DEFAULT 'user_initiated'
        CHECK (discovery_origin IN ('user_initiated','pulse','recommender','citation_batch'));

-- Backfill: papers with a pulse_cards row → 'pulse'
UPDATE papers SET discovery_origin = 'pulse'
WHERE id IN (SELECT DISTINCT paper_id FROM pulse_cards);

-- Papers with a paper_recommendations row but no pulse_cards row → 'recommender'
UPDATE papers SET discovery_origin = 'recommender'
WHERE id IN (SELECT DISTINCT paper_id FROM paper_recommendations)
  AND discovery_origin = 'user_initiated';

-- All others stay 'user_initiated' (default)

COMMIT;
```

**Set-on-insert rule:** every code path that INSERTs into `papers` must specify `discovery_origin`:
- `pulse/job.py::run_pulse` → `'pulse'`
- `recommender.py::refresh_recommendations` → `'recommender'`
- `routers/search.py` (search-then-save) → `'user_initiated'`
- `routers/pdf.py` (upload) → `'user_initiated'`
- `integrations/zotero_service.py` (sync) → `'user_initiated'`
- `routers/papers.py::batch_save_papers` → `'citation_batch'`

Once set, the column is **immutable** (no UPDATE allowed in business code). The first system to fetch the paper "wins."

### 3.3 Migration 049 — `recommendation_feedback` table + drop `pulse_ratings`

```sql
BEGIN;

CREATE TABLE recommendation_feedback (
    id           BIGSERIAL PRIMARY KEY,
    paper_id     BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    user_id      BIGINT REFERENCES users(id) ON DELETE CASCADE,  -- NULL = single-tenant
    signal       TEXT NOT NULL CHECK (signal IN ('positive','negative')),
    source       TEXT NOT NULL CHECK (source IN (
        'pulse_thumbs',          -- 👍/👎 on Pulse Deck card
        'feed_thumbs',           -- 👍/👎 on Inbox/Library row (Pulse-origin only)
        'paper_detail_thumbs',   -- 👍/👎 on Paper Detail page
        'dismiss_combined'       -- 🗑+👎 combined button
    )),
    topic_id     BIGINT REFERENCES topics(id) ON DELETE SET NULL,
    reason       TEXT,                                  -- optional free-text (Paper Detail only)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE NULLS NOT DISTINCT (paper_id, user_id, source)  -- replace via upsert
);

CREATE INDEX recommendation_feedback_paper_idx ON recommendation_feedback (paper_id);
CREATE INDEX recommendation_feedback_signal_recent_idx
    ON recommendation_feedback (signal, created_at DESC);
CREATE INDEX recommendation_feedback_topic_idx
    ON recommendation_feedback (topic_id) WHERE topic_id IS NOT NULL;

-- Migrate existing pulse_ratings into recommendation_feedback BEFORE dropping the source table.
-- pulse 'save'  → lifecycle-only in new model, NOT feedback. Skip.
-- pulse 'open'  → no signal in new model. Skip.
-- pulse 'up'    → positive thumbs.
-- pulse 'down'  → negative thumbs.
-- pulse 'dismiss' → was lifecycle+feedback combined → maps to 'dismiss_combined'.
INSERT INTO recommendation_feedback (paper_id, user_id, signal, source, created_at)
SELECT
    pr.paper_id,
    pr.user_id,
    CASE WHEN pr.rating = 'up' THEN 'positive'
         WHEN pr.rating IN ('down','dismiss') THEN 'negative' END AS signal,
    CASE WHEN pr.rating IN ('up','down') THEN 'pulse_thumbs'
         WHEN pr.rating = 'dismiss' THEN 'dismiss_combined' END AS source,
    pr.created_at
FROM pulse_ratings pr
WHERE pr.rating IN ('up','down','dismiss')
ON CONFLICT (paper_id, user_id, source) DO NOTHING;

-- Drop the legacy table — single source of truth going forward.
-- Pulse-internal analytics (deck composition, rating distribution) is
-- reconstructable from pulse_decks + pulse_cards JOIN recommendation_feedback
-- by (paper_id, created_at::date). No data lost; just deduplicated.
DROP TABLE pulse_ratings;

COMMIT;
```

**Single source of truth** post-049: `recommendation_feedback` is the only table that records user-quality signals. Pulse stage-2 reranker, recommender filter, topic dampening — all read from this one table. The `pulse_ratings` analytics use cases (rare; mostly debug-only today) are reconstructable via the natural join above.

### 3.4 State-on-create rules per entry path

The discovery_origin determines whether feedback buttons appear; the initial `state` determines which surface the paper lands on. These are independent:

| Entry path | `papers.discovery_origin` | Initial `paper_user_state.state` | Initial surface |
|---|---|---|---|
| Pulse fetch (overnight discovery) | `pulse` | (no row → defaults to `inbox` via COALESCE) | Inbox + Pulse Deck |
| Recommender output | `recommender` | (no row → defaults to `inbox`) | Inbox |
| Citation batch save | `citation_batch` | `to_read` | Library / Reading List |
| Manual search → save | `user_initiated` | `to_read` | Library |
| PDF upload | `user_initiated` | `to_read` | Library |
| **Zotero sync** | `user_initiated` | **`to_read`** | **Library** (NOT Inbox — see §11b) |
| Telegram /papers + bookmark callback | inherits paper's existing origin | `to_read` + `starred=TRUE` | Library / Starred |

**Rule of thumb:** if the user *actively chose* the paper (explicit save, upload, Zotero clip, citation graph save) → `state='to_read'`. If the system fetched it on the user's behalf (Pulse, recommender) → no row, paper appears in Inbox by default.

---

## 4. Action Contract

Every user-facing action maps to one or more writes. The spec pins **which concern each action touches**.

### 4.1 Lifecycle actions

| Action | Endpoint | Writes |
|---|---|---|
| Save | `PUT /api/papers/{id}/save` | `state = 'to_read'`. Does NOT write feedback. |
| Skip | `PUT /api/papers/{id}/skip` | `state = 'done'`. Does NOT write feedback. (For "not for me, no signal needed" inbox triage.) |
| Mark Reading | `PUT /api/papers/{id}/reading` | `state = 'reading'`. (Auto-called when Paper Detail opens for a `to_read` paper.) |
| Mark Done | `PUT /api/papers/{id}/done` | `state = 'done'`. |
| Trash | `PUT /api/papers/{id}/trash` | `state_before_trash = state; state = 'trash'`. |
| Restore | `PUT /api/papers/{id}/restore` | `state = state_before_trash; state_before_trash = NULL`. |
| Hard Delete | `DELETE /api/papers/{id}` | precondition `state='trash'`; (1) inside `conn.transaction()`: `DELETE FROM papers WHERE id=$1` (cascades through FKs per init.sql); (2) AFTER commit: `delete_paper_vectors(id)` as best-effort Qdrant cleanup. If Qdrant cleanup raises, log and (optionally) enqueue an orphan-vectors reaper job — the paper is already gone from PG. **Order rationale:** if SQL DELETE fails (FK violation, lock timeout), the txn rolls back and Qdrant is untouched → user can retry. If SQL succeeds and Qdrant fails, we have orphan vectors but the paper is gone (recoverable via reaper). The reverse order — Qdrant-then-SQL — is data-loss-prone (audit finding NEW-H2 from 2026-04-30 deep-audit). |

### 4.2 Curation actions

| Action | Endpoint | Writes |
|---|---|---|
| Star | `PUT /api/papers/{id}/star` | `starred = TRUE`. Does NOT change `state`. |
| Unstar | `PUT /api/papers/{id}/unstar` | `starred = FALSE`. |

### 4.3 Feedback actions

| Action | Endpoint | Writes |
|---|---|---|
| 👍 Useful | `POST /api/papers/{id}/feedback` body `{signal:'positive', source:<context>}` | INSERT/UPSERT into `recommendation_feedback`. Does NOT change `state` or `starred`. |
| 👎 Not for me | `POST /api/papers/{id}/feedback` body `{signal:'negative', source:<context>}` | INSERT/UPSERT into `recommendation_feedback`. Does NOT change `state` or `starred`. |
| Reason (free-text) | `POST /api/papers/{id}/feedback` body `{signal, source, reason}` | Same + populate `reason`. |

### 4.4 Combined actions

| Action | Endpoint | Writes |
|---|---|---|
| 🗑+👎 Trash & reject | `PUT /api/papers/{id}/trash_and_reject` | Single transaction: lifecycle write (`state='trash'`, `state_before_trash=state`) + feedback write (`signal='negative', source='dismiss_combined'`). Returns combined response. |

**Save is lifecycle-only (locked by user 2026-04-29).** Clicking 💾 anywhere — including on a Pulse card — writes ONLY `state = 'to_read'`. No feedback row is created. If the user wants to express positive feedback on a Pulse-recommendation, they click 👍 separately (which is also visible on the same Pulse card). This is the deliberate consequence of the three-concerns separation: Save = lifecycle, 👍 = feedback, and they don't bleed into each other.

The combined `🗑+👎` button is the **only** combined action in the system. It exists because "trash this AND don't recommend more like it" is a high-frequency user intent that deserves a one-click affordance. The frontend renders it on Pulse-origin papers only (Pulse cards, Inbox rows from Pulse-origin papers, etc.).

### 4.5 Bulk actions

`POST /api/papers/bulk` with `{paper_ids: [...], action: 'save'|'skip'|'trash'|'mark_reading'|'mark_done'|'restore'|'star'|'unstar'|'feedback_positive'|'feedback_negative'}`.

Per-paper transaction; returns `{succeeded: [], failed: [{paper_id, error}]}`. No combined `trash_and_reject` in bulk — the convenience is single-row-only.

### 4.6 Endpoints REMOVED (deprecation)

- `PUT /api/papers/{id}/bookmark` — replaced by Star + Save (separate concerns).
- `PUT /api/papers/{id}/read` — replaced by `Mark Done`.
- `PUT /api/papers/{id}/archive` — semantics absorbed into `Mark Done`.
- `PUT /api/papers/{id}/dismiss` — replaced by `Trash` (lifecycle only) and optional `🗑+👎` combo.
- `PUT /api/papers/{id}/unsave` — undo via `Trash` or by transitioning state explicitly. Removed.

These endpoints are **REMOVED in the same commit that lands the new endpoints in §8** — no 410 Gone aliases, no transitional message body, no deprecation period. See §11 (Backwards-Incompatible Cutover) for the atomic-cutover rule that governs this. Any legacy callers (e.g. cached browser bookmarks, old Telegram digest links) will receive plain 404 — acceptable pre-launch.

---

## 5. Surface Placement

### 5.1 Lifecycle action buttons

| Surface | Buttons shown | Notes |
|---|---|---|
| Inbox row (any origin) | 💾 Save · ⏩ Skip · 🗑 Trash · ⭐ Star | Always available |
| Reading List (`to_read`) row | 📖 Mark Reading · ✓ Mark Done · 🗑 Trash · ⭐ Star | |
| Reading row | 📚 Set Aside (→ to_read) · ✓ Mark Done · 🗑 Trash · ⭐ Star | |
| Done row | ↺ Re-open (→ to_read) · 🗑 Trash · ⭐ Star | |
| Trash row | ↩ Restore · ❌ Hard Delete | Hard Delete is keyboard-confirmable; no title-type modal (spec'd in old contract — removed in this redesign). |
| Paper Detail header | Lifecycle action contextual to current state, plus Star toggle | One-click is enough; no menu |

**Frontend icon mapping (lucide-react):**
| Spec emoji | Lucide icon | Use |
|---|---|---|
| 💾 Save | `<Save />` or `<Bookmark />` | Save button |
| ⏩ Skip | `<FastForward />` | Inbox skip |
| 📖 Mark Reading | `<BookOpen />` | Reading List → start reading |
| 📚 Set Aside | `<Library />` | Reading → back to to_read |
| ✓ Mark Done | `<CheckCircle />` | Mark Done |
| 🗑 Trash | `<Trash2 />` | Trash transition |
| ↩ Restore | `<ArchiveRestore />` | Trash → restore |
| ↺ Re-open | `<RotateCcw />` | Done → re-open |
| ❌ Hard Delete | `<Trash />` (filled) | Trash → permanent delete |
| ⭐ Star / Unstar | `<Star />` / `<StarOff />` | Curation flag toggle |
| 👍 Useful | `<ThumbsUp />` | Positive feedback |
| 👎 Not for me | `<ThumbsDown />` | Negative feedback |
| 🗑+👎 Trash & reject | `<Trash2 />` with `<ThumbsDown />` overlay or labeled "Trash & don't recommend" | Combined action button |

### 5.2 Feedback button placement (origin-conditional)

Feedback buttons appear **only** when `papers.discovery_origin != 'user_initiated'`.

| Surface | Origin = `pulse` / `recommender` / `citation_batch` | Origin = `user_initiated` |
|---|---|---|
| Pulse Deck card | 👍 / 👎 / 💾 / 🗑+👎 | (N/A — Pulse cards are always Pulse-origin) |
| Pulse Preview (My Day) | 👍 / 👎 / 💾 | (N/A) |
| Inbox row | 👍 / 👎 added to lifecycle button group | none |
| Reading List / Reading / Done row | 👍 / 👎 added to lifecycle button group | none |
| Paper Detail sidebar | 👍 / 👎 + optional reason free-text | none |
| Trash row | none (avoid double-prompting if combined trash already wrote signal) | none |

### 5.3 Telegram parity

| Surface | Lifecycle | Feedback |
|---|---|---|
| `/inbox` command rows | 💾 Save · 🗑 Trash · 🗑+👎 (Pulse-origin only) | 👍 / 👎 (Pulse-origin only) |
| `/papers` command rows | ⭐ Star toggle · 🗑 Trash | (no feedback for plain library) |
| `/next` command (single Pulse card) | 💾 Save · 🗑 Trash · 🗑+👎 | 👍 / 👎 |
| `/pulse_now` deck rows | (same as Pulse Deck card) | (same) |
| Paper Detail (callback) | (state-contextual) | (origin-conditional) |

Callback names follow the convention `paper:<action>:<id>`:
- `paper:save:<id>`, `paper:skip:<id>`, `paper:reading:<id>`, `paper:done:<id>`, `paper:trash:<id>`, `paper:restore:<id>`
- `paper:star:<id>`, `paper:unstar:<id>`
- `paper:feedback_pos:<id>:<source>`, `paper:feedback_neg:<id>:<source>`
- `paper:trash_reject:<id>` (combined)

Existing callbacks (`paper_bookmark_<id>`, `pulse_up_<id>`, etc.) deprecated in lockstep with the endpoints they call.

### 5.4 Surface chips on `/feed`

| Chip | Predicate | Sub-chips |
|---|---|---|
| Inbox | `state = 'inbox'` | (none — origin filter is hidden behavior) |
| Library | `state IN ('to_read','reading','done')` | All · ⭐ Starred · ▶ Reading · ⏸ Reading List · ✓ Done |
| Search | (search bar input) | n/a |
| Ask | (RAG chat surface) | n/a |
| Trash | `state = 'trash'` | n/a |

Pulse Deck stays at `/pulse` (unchanged). My Day shows Pulse Preview widget.

---

## 6. View Predicates (Final)

```python
# services/paper_ingestion/paper_ingestion/queries/predicates.py

VIEW_PREDICATES: dict[str, str] = {
    "inbox":         "pus.state = 'inbox'",
    "library":       "pus.state IN ('to_read','reading','done')",
    "reading_list":  "pus.state = 'to_read'",
    "reading":       "pus.state = 'reading'",
    "done":          "pus.state = 'done'",
    "starred":       "pus.starred = TRUE AND pus.state != 'trash'",
    "trash":         "pus.state = 'trash'",
    "active":        "pus.state IN ('inbox','to_read','reading')",
    "kept":          "pus.state IN ('to_read','reading','done')",
    "all_non_trash": "pus.state != 'trash'",
}

# Recommender exclusion (one line)
RECOMMENDER_EXCLUDE_SQL = "pus.state IN ('trash','done')"

# Pulse candidate filter (one line)
PULSE_CANDIDATE_EXCLUDE_SQL = "COALESCE(pus.state, 'inbox') IN ('trash','done')"
```

**LEFT JOIN behavior:** for every view, papers without a `paper_user_state` row are treated as `state='inbox'` via `COALESCE(pus.state, 'inbox')` in the JOIN clause. This handles freshly-discovered papers correctly (Inbox = "no row OR state='inbox'").

---

## 7. Backend Learning Loop (L1 + L2 + L3)

The `recommendation_feedback` table feeds three layers of consumption. All three ship in this redesign per user lock-in (2026-04-29).

### 7.1 L1 — LLM prompt enrichment (stage-2 reranker)

**Today's mechanic, broader source.** Replace `pulse_ratings` lookup in `pulse/profile.py::load_profile` with `recommendation_feedback` lookup. Pass to stage-2 LLM scorer:

```python
# pulse/profile.py
async def load_profile(...) -> Profile:
    ...
    negative_topics = await conn.fetch("""
        SELECT t.name, COUNT(*) AS cnt
          FROM recommendation_feedback rf
          JOIN papers p ON p.id = rf.paper_id
          JOIN paper_topics pt ON pt.paper_id = p.id
          JOIN topics t ON t.id = pt.topic_id
         WHERE rf.signal = 'negative'
           AND rf.created_at > NOW() - INTERVAL '90 days'
           AND rf.user_id IS NOT DISTINCT FROM $1
         GROUP BY t.name
         ORDER BY cnt DESC
         LIMIT 10
    """, user_id)
    negative_authors = await conn.fetch("...")  # similar shape
    negative_titles = await conn.fetch("...")    # broader than today
    ...
```

Stage-2 prompt template gains explicit negative context:
> The user has rejected papers about: [topic1, topic2, ...]. They have downvoted authors: [author1, author2, ...]. Avoid recommending papers that match these patterns.

**Impact:** ~30% effectiveness baseline (LLM may or may not honour soft constraints).

### 7.2 L2 — Cosine penalty (stage-1 embedding filter)

**Deterministic, immediate.** Stage-1 cosine score becomes:

```python
# pulse/scoring.py::stage1_embedding_filter
positive_centroid = mean(library_papers.embedding for library_papers in liked_papers)
negative_centroid = mean(papers.embedding for paper_id IN (
    SELECT paper_id FROM recommendation_feedback
    WHERE signal = 'negative'
      AND created_at > NOW() - INTERVAL '90 days'
      AND user_id IS NOT DISTINCT FROM current_user_id
))

# Time-decay: negatives older than 30 days get half-weight in centroid
score = cosine(candidate.embedding, positive_centroid) - LAMBDA * cosine(candidate.embedding, negative_centroid)
# LAMBDA tunable in user_config; default 0.5
```

If `negative_centroid` is undefined (zero negatives), penalty term is 0.

**Impact:** ~70% effectiveness — papers semantically similar to 👎'd ones get demoted in the candidate pool deterministically. Visible to user as "Pulse contains fewer papers like X tomorrow."

### 7.3 L3 — Deterministic exclusion + topic dampening + Settings UI

**The full closed loop, with user-visible state.**

#### 7.3.1 Recommender hard exclusion

```python
# recommender.py::_filter_unread
WHERE state IN ('trash','done')
   OR EXISTS (
       SELECT 1 FROM recommendation_feedback rf
        WHERE rf.paper_id = p.id
          AND rf.signal = 'negative'
          AND rf.created_at > NOW() - INTERVAL '60 days'
          AND rf.user_id IS NOT DISTINCT FROM $current_user_id
   )
```

A paper directly 👎'd is excluded from the candidate pool for 60 days. Time-decay because user preferences evolve.

#### 7.3.2 Topic dampening

If ≥5 negative feedbacks reference the same `topic_id` within the last 90 days, demote that topic in user's profile:

```python
# pulse/profile.py
topic_dampening = await conn.fetch("""
    SELECT t.id, t.name, COUNT(*) AS neg_count
      FROM recommendation_feedback rf
      JOIN paper_topics pt ON pt.paper_id = rf.paper_id
      JOIN topics t ON t.id = pt.topic_id
     WHERE rf.signal = 'negative'
       AND rf.created_at > NOW() - INTERVAL '90 days'
       AND rf.user_id IS NOT DISTINCT FROM $1
     GROUP BY t.id, t.name
    HAVING COUNT(*) >= 5
""", user_id)

# Apply: each dampened topic's contribution to topic-bonus score in stage-1 → 0.5x
```

#### 7.3.3 Settings UI

Settings → Pulse → "Topics you've rejected" panel lists each dampened topic with:
- Negative-feedback count in the last 90 days
- Sample paper titles that triggered the dampening
- "Reset" button → DELETE all `recommendation_feedback` rows for that topic_id (clean reset, not just hide)

Provides user control + audit trail. Solves the "I made a mistake, how do I un-reject medical papers?" use case.

#### 7.3.4 Risk mitigation

L3 may over-prune if a user 👎s many topics (e.g., a researcher with narrow focus). Two safeguards:

1. **Minimum candidate guarantee:** if exclusion would leave fewer than 20 candidates for a Pulse run, log warning and skip the L3 hard-exclusion step (fall back to L1+L2 only).
2. **Topic-dampening cap:** at most 50% of topics can be dampened per user; further dampenings are queued but don't apply until cap clears.

---

## 8. API Surface (Final)

| Method | Path | Body | Purpose | Rate-limit |
|---|---|---|---|---|
| `PUT` | `/api/papers/{id}/save` | none | state → `to_read` | 60/min |
| `PUT` | `/api/papers/{id}/skip` | none | state → `done` (no signal) | 60/min |
| `PUT` | `/api/papers/{id}/reading` | none | state → `reading` | 60/min |
| `PUT` | `/api/papers/{id}/done` | none | state → `done` | 60/min |
| `PUT` | `/api/papers/{id}/trash` | none | state_before = state; state → `trash` | 60/min |
| `PUT` | `/api/papers/{id}/restore` | none | state → state_before; clear state_before | 60/min |
| `PUT` | `/api/papers/{id}/trash_and_reject` | none | combined: state → trash + feedback negative | 30/min |
| `PUT` | `/api/papers/{id}/star` | none | starred = TRUE | 60/min |
| `PUT` | `/api/papers/{id}/unstar` | none | starred = FALSE | 60/min |
| `POST` | `/api/papers/{id}/feedback` | `{signal, source, reason?}` | INSERT/UPSERT recommendation_feedback | 60/min |
| `DELETE` | `/api/papers/{id}` | none | state must = `trash`; cascade + Qdrant cleanup | 10/min |
| `POST` | `/api/papers/bulk` | `{paper_ids, action}` | bulk transition | 20/min |
| `GET` | `/api/papers/feed` | `?surface=&filter=&limit=&offset=` | feed listing | 60/min |
| `GET` | `/api/papers/feed/counts` | none | per-surface counts | 60/min |
| `GET` | `/api/papers/{id}` | none | paper detail; response includes `discovery_origin` for frontend conditionals | 60/min |
| `GET` | `/api/recommendation_feedback` | `?paper_id=` (optional) | list user's recent feedback (for Settings UI) | 30/min |
| `DELETE` | `/api/recommendation_feedback?topic_id={id}` | none | reset all feedback for a topic (Settings UI) | 5/min |

---

## 9. Frontend Component Contract

### 9.1 `UserStateResponse` shape (post-redesign)

```typescript
export interface UserStateResponse {
  state: 'inbox' | 'to_read' | 'reading' | 'done' | 'trash';
  state_before_trash: 'inbox' | 'to_read' | 'reading' | 'done' | null;
  starred: boolean;
  rating: number | null;          // 1-5, separate from feedback (subjective quality)
  user_notes: string | null;
  flagged: boolean;
  updated_at: string;
}

export interface PaperResponse {
  id: number;
  title: string;
  // ... existing fields ...
  discovery_origin: 'user_initiated' | 'pulse' | 'recommender' | 'citation_batch';
  user_state: UserStateResponse | null;
  recent_feedback: {
    signal: 'positive' | 'negative';
    source: string;
    created_at: string;
  } | null;  // most recent feedback by current user (for UI affordance state)
}
```

### 9.2 Components affected

- **`FeedPaperRow.tsx`** — accepts `paper.discovery_origin` and conditionally renders feedback buttons. Replaces lifecycle-button logic with `state`-based switch.
- **`PaperHeader.tsx`** + **`ActionsSidebar.tsx`** — same conditional logic.
- **`BulkToolbar.tsx`** — surface-aware actions stay; remove `unsave` and `archive` from action list.
- **`PulseCard.tsx`** — gains `🗑+👎` button.
- **New: `FeedbackButtons.tsx`** — shared component rendered conditionally where `paper.discovery_origin !== 'user_initiated'`.
- **New: `RejectedTopicsPanel.tsx`** — Settings → Pulse → "Topics you've rejected." Lists dampened topics with reset button.

### 9.3 Surface chips

`ResearchFeedPage.tsx` chip taxonomy:
- Top: **Inbox · Library · Search · Ask · Trash** (5 surface chips)
- Library sub-chips: **All · ⭐ Starred · ▶ Reading · ⏸ Reading List · ✓ Done**

URL params: `?surface=<inbox|library|search|ask|trash>` and `?filter=<starred|reading|to_read|done>` for Library sub-filters.

---

## 10. Out of Scope (Explicitly Deferred)

| Item | Why deferred |
|---|---|
| **Multi-tenant write enforcement** | `current_user_id_or_none` resolver still NULL-only (M1 in known-residual-risks). Spec uses single-tenant assumptions; multi-tenant migration is a future sprint. |
| **Per-author feedback** | Author-level negative signals could be derived from `recommendation_feedback` JOIN `paper_authors`, but a dedicated UI ("don't recommend papers by author X") is deferred. |
| **Cross-user collaborative filtering** | Single-tenant only for now. |
| **Topic auto-discovery from clusters** | Current topic taxonomy is user-defined. Auto-clustering from embeddings is a separate research project. |
| **Embedding fine-tuning from feedback** | We tune retrieval (centroid math), not the embedding model itself. |
| **Free-text reason mining** | `recommendation_feedback.reason` collected but not yet analyzed. Future LLM-based pattern extraction. |
| **`paper_user_state.discovery_origin` (per-user origin)** | `papers.discovery_origin` is per-paper. If multi-tenant ever ships, a follow-up migration can move origin to per-(paper, user) granularity. |
| **`zotero.remove` job handler** | This redesign does NOT auto-remove from Zotero on JARVIS Trash/Hard-Delete. Zotero is the user's permanent reference library; mirroring deletes is a destructive operation that needs explicit user consent + confirmation flow. Deferred to a dedicated Zotero-sync sprint. |

---

## 11. Backwards-Incompatible Cutover (no shims, no aliases)

**Pre-launch product, no public users → clean cut.** Carrying transitional aliases would add documentation rot, test maintenance burden, and reader confusion ("why is this 410-Gone code here?"). The migration is atomic across all layers in a single sprint.

| Layer | Cutover rule |
|---|---|
| **Schema** | Migrations 047/048/049 drop legacy columns (`saved`, `dismissed`, `archived`, `status`, `preference`) and tables (`pulse_ratings`) in one transaction. No transitional period. |
| **Backend endpoints** | Removed in same commit they're replaced: `/bookmark`, `/read`, `/archive`, `/dismiss`, `/unsave`, `/feedback` (old shape). New endpoints (§8) are the only API surface after cutover. |
| **Telegram callbacks** | Old callback names (`paper_bookmark_<id>`, `pulse_up_<id>`, `pulse_down_<id>`, `pulse_save_<id>`) removed. New convention `paper:<action>:<id>` (§5.3) is the only handler. Old callbacks pinned in users' Telegram clients (from old digests) will silently fail — acceptable pre-launch. |
| **Frontend** | All components updated atomically. No dual-shape branches. `UserStateResponse.saved`/`archived`/`dismissed` removed from types/index.ts. |
| **Tests** | All test fixtures rewritten to new shape. No "legacy compatibility" tests. |
| **Documentation** | README, PRD, AGENTS.md, CLAUDE.md, CHANGELOG.md, REQUIREMENTS.md, ARCHITECTURE.md, DEPLOYMENT.md all updated in the same sprint. Old contract docs (`paper-lifecycle-contract.md`, `feed-information-architecture.md`) deleted (not deprecated — deleted) and references repointed to this spec. |
| **Code comments** | Any in-code comment referencing `saved`, `dismissed`, `archived`, `bookmark`, `preference`, or `pulse_ratings` either rewritten or removed. |

**Single-sprint constraint:** the migration is too entangled to split across sprints. Schema, backend, frontend, Telegram, and docs all reference the same shape and must change together. The implementation plan must explicitly coordinate this — no half-landed states.

---

## 11b. Zotero Interplay

Zotero is the user's *permanent reference library*. JARVIS state is *workflow* (where am I in reading this paper). These are different concerns and the redesign keeps them deliberately separate.

### 11b.1 Sync direction: Zotero → JARVIS

When the Zotero sync job (`integrations/zotero_service.py::_handle_zotero_items`) ingests a paper:

| Field | Value |
|---|---|
| `papers.discovery_origin` | `'user_initiated'` (the user actively clipped it in Zotero) |
| `papers.source_type` | `'local'` (existing convention; Zotero papers are tagged this way) |
| `papers.zotero_item_key` | the Zotero item key (existing) |
| `paper_user_state.state` | **`'to_read'`** (auto-created at sync time) |
| `paper_user_state.starred` | `FALSE` (the user can star later if they want push-back to Zotero) |

**Why `to_read` and not `inbox`:** the user already triaged the paper externally (clipped it to Zotero). Forcing them to re-triage in JARVIS Inbox is redundant. Zotero clip = "yes, save this" = `to_read`.

**No feedback buttons on Zotero papers** (§5.2 rule applies: `discovery_origin = 'user_initiated'` → feedback hidden).

### 11b.2 Push direction: JARVIS → Zotero

Trigger preserved from current behavior:

```
starred = TRUE  AND  exists(project_paper link)  →  enqueue zotero.push job
```

**Why starred (not Save):** the existing semantics — "this paper is important enough to add to my permanent reference manager" — fits Star, not Save. Save is JARVIS-internal triage; Star is "this is worth keeping forever." A user might Save 200 papers in JARVIS but only Star 20 they consider truly important; only those 20 get pushed to Zotero.

The trigger is unchanged from today's logic in `routers/dashboard_api.py::upsert_user_state`. The redesign repoints the trigger to fire on the new `PUT /api/papers/{id}/star` endpoint instead of on the old user-state PUT.

### 11b.3 Lifecycle transitions and Zotero

| JARVIS lifecycle event | Zotero side-effect | Reasoning |
|---|---|---|
| Save (any state → `to_read`) | none | Save is JARVIS-internal triage. User can Star separately if they want Zotero push. |
| Mark Reading | none | Reading progress doesn't belong in a reference manager. |
| Mark Done | none | Done means finished reading; the paper still belongs in Zotero as a citation. |
| Star (curation flag → TRUE) | enqueue `zotero.push` if project-linked | Existing trigger preserved. |
| Unstar (curation flag → FALSE) | none | Unstarring does NOT remove from Zotero. The user's reference library is sacred — JARVIS won't destroy citations they may have already cited in papers. |
| Trash (any state → `'trash'`) | none | JARVIS trash ≠ Zotero remove. The user's permanent library is independent of JARVIS workflow state. |
| Restore (`'trash'` → state_before) | none | Symmetric with Trash. |
| Hard Delete | none in this sprint | `zotero.remove` handler is deferred (§10). The Hard Delete confirmation modal does NOT offer "also remove from Zotero" — that's a future feature. |

### 11b.4 Settings UI surface

Settings → Integrations → Zotero panel shows:
- Connection status (existing)
- Last sync time (existing)
- **NEW:** "Auto-sync papers from Zotero land in your Reading List." (informational, no toggle — this is the design)
- **NEW:** "Push to Zotero when a paper is starred and linked to a project." (informational, no toggle — this is the existing behavior, made explicit)

**No new toggles.** The behavior is opinionated and matches researcher mental model. Future sprints can add toggles if real users request them.

### 11b.5 Edge cases

- **Paper synced from Zotero, then user Stars it:** `discovery_origin='user_initiated'`. Star toggles starred=TRUE; push trigger fires (already in Zotero, but `zotero.push` is idempotent — updates the existing item). No duplicate Zotero entry.
- **Paper from Pulse that user later sees in Zotero (manually clipped):** `discovery_origin='pulse'` (set on first insert; immutable). Zotero sync sees the paper already exists by DOI; updates `papers.zotero_item_key`. JARVIS state unchanged. Feedback buttons remain visible (origin still says `pulse`).
- **User trashes a paper that's in their Zotero:** paper stays in Zotero. JARVIS state='trash'. If the user Restores later, JARVIS state returns; Zotero unaffected throughout.

---

## 12. Test Strategy

- **Migration 047 live-PG test:** apply through 046, INSERT rows in every legal pre-redesign combination, apply 047, assert `state` mapping + `state_before_trash` correctness for trash rows.
- **Migration 048 live-PG test:** verify backfill correctly tags papers with `pulse_cards` rows as `pulse`, papers with `paper_recommendations` rows as `recommender`, others as `user_initiated`.
- **Migration 049 live-PG test:** verify `pulse_ratings` migration into `recommendation_feedback`, deduplication, indexes.
- **Endpoint integration tests:** every action in §8 has at least 3 tests (happy path, idempotent re-call, error path).
- **Recommender unit tests:** verify L2 cosine penalty math; L3 exclusion at 60-day boundary; topic dampening threshold.
- **Frontend component tests:** `FeedbackButtons` shows iff `discovery_origin != 'user_initiated'`; `FeedPaperRow` renders state-correct lifecycle buttons; `RejectedTopicsPanel` reset flow.
- **Playwright E2E lifecycle:** `inbox → save → reading_list → mark_reading → done → trash → restore → re-trash → hard_delete`. Plus origin-conditional feedback button visibility tested with seeded papers of each origin type.
- **Playwright E2E feedback loop:** seed paper with origin='pulse'. Click 👎 on Inbox row. Re-run Pulse. Assert that paper does not appear in next deck (L3 exclusion visible).

---

## 13. Acceptance Criteria

The redesign is complete when:

1. Migration 047, 048, 049 all apply cleanly forward and produce expected backfilled state.
2. All deprecated endpoints (`/bookmark`, `/read`, `/archive`, `/dismiss`, `/unsave`) are REMOVED — no 404, no 410, they simply do not exist in the router. Same for deprecated callbacks. Old contract docs deleted from the repo.
3. Every component listed in §9.2 renders origin-conditional feedback buttons correctly.
4. Recommender excludes 👎'd papers for 60 days (verified via integration test).
5. Pulse deck composition reflects L2 cosine penalty (verified via fixture-based test).
6. Settings UI "Topics you've rejected" panel lists dampened topics and reset works.
7. Telegram `/inbox`, `/papers`, `/next`, `/pulse_now` all use new endpoints; callback names match new convention.
8. README, PRD, AGENTS.md, CLAUDE.md, CHANGELOG.md all reflect new lifecycle.
9. Old contract docs (`paper-lifecycle-contract.md`, `feed-information-architecture.md`) DELETED from the repo (per §11 — not retained as deprecated stubs).
10. Hard-delete ordering verified: regression test mocks `conn.execute("DELETE FROM papers ...")` to raise and asserts `delete_paper_vectors` was NOT called (rolls back cleanly). Second test asserts that after a successful SQL commit, a Qdrant exception is logged but does not propagate.
11. Quality gates pass: pyright 0/0, all backend + frontend tests green, e2e:mocked green.

---

## 14. Verified Identifiers

Every identifier cited in this spec was either Read in this session via the audit agents or is a NEW identifier introduced by the spec.

| Citation | File:line | Behavior |
|---|---|---|
| `paper_user_state` (post-046) | db/init.sql:247-286 + migration 046 | Current shape with saved/dismissed/archived/status/preference columns |
| `pulse_ratings` table | db/init.sql:740-741 | CHECK rating IN (up,down,save,dismiss,open) — DROPPED in migration 049 |
| Zotero sync entry path | services/paper_ingestion/paper_ingestion/integrations/zotero_service.py:515-537 | Creates papers row with source_type=LOCAL, external_id="zotero:{key}"; today does NOT create paper_user_state row — redesign requires it to create row with state='to_read' |
| Zotero push trigger | services/paper_ingestion/paper_ingestion/routers/dashboard_api.py:104-181 | upsert_user_state currently triggers zotero.push when starred=TRUE + project link exists; redesign repoints trigger to PUT /api/papers/{id}/star |
| `pulse_cards` table | db/init.sql:706-720 | Links paper_id to pulse_decks; ON DELETE CASCADE |
| `paper_recommendations` table | (existing — recommender output) | Recommender writes here; consumed by Telegram /next + dashboard |
| `_filter_unread` | services/paper_ingestion/paper_ingestion/ingestion/recommender.py:175-197 | Today excludes status='read' OR archived OR dismissed; ignores preference |
| `_persist_deck_inner` | services/paper_ingestion/paper_ingestion/pulse/deck.py:43-143 | Filters by `NOT IS_ARCHIVED AND NOT dismissed` per user |
| `load_profile` | services/paper_ingestion/paper_ingestion/pulse/profile.py:49-228 | Builds positive centroid from starred/read/reading; reads pulse_ratings for ±titles |
| `stage1_embedding_filter` | services/paper_ingestion/paper_ingestion/pulse/scoring.py:94-206 | Cosine + topic + recency + author bonus |
| `stage2_llm_rerank` | services/paper_ingestion/paper_ingestion/pulse/scoring.py:214-321 | LLM scores using ±titles in prompt |
| `rate_card` | services/paper_ingestion/paper_ingestion/routers/pulse.py:119-216 | Maps rating → pulse_ratings + paper_user_state |
| `bookmark_paper` (deprecated) | services/paper_ingestion/paper_ingestion/routers/papers.py:335-387 | Toggle starred + saved=TRUE on star (asymmetric) |
| `archive_paper` (deprecated) | services/paper_ingestion/paper_ingestion/routers/papers.py:395-437 | archived; requires saved=TRUE |
| `dismiss_paper` (deprecated) | services/paper_ingestion/paper_ingestion/routers/papers.py:737-758 | dismissed=TRUE, preference='down' |
| `restore_paper` (transitional) | services/paper_ingestion/paper_ingestion/routers/papers.py:766-787 | dismissed=FALSE, preference='none' — semantics change in redesign |
| `paper-lifecycle-contract.md` | docs/specs/paper-lifecycle-contract.md | DELETED by this spec (per §11 — clean cut, no deprecated stub) |
| `feed-information-architecture.md` | docs/specs/feed-information-architecture.md | DELETED by this spec (per §11 — clean cut, no deprecated stub) |
| `FeedPaperRow.tsx` | frontend/src/components/feed/FeedPaperRow.tsx:8-345 | Reads user_state booleans; rewrite to read state enum |
| `PaperHeader.tsx` | frontend/src/components/paper/PaperHeader.tsx:17-186 | Action bar with all lifecycle + feedback affordances |
| `BulkToolbar.tsx` | frontend/src/components/feed/BulkToolbar.tsx:12-79 | 8 bulk actions per surface; rewrite for new states |
| `ResearchFeedPage.tsx` | frontend/src/pages/ResearchFeedPage.tsx:37-291 | Surface chips + sub-chips |
| `paper_save_callback` | services/telegram_bot/.../callback_handler.py:107-143 | Callback for paper:save (currently writes saved=TRUE) |
| `paper_dismiss_callback` | services/telegram_bot/.../callback_handler.py:147-184 | Callback for paper:dismiss (currently writes dismissed=TRUE) |
| `pulse_rating_callback` | services/telegram_bot/.../callback_handler.py:298-345 | Callback for pulse_up/down/save |
| `papers.discovery_origin` | NEW — migration 048 | ENUM ('user_initiated','pulse','recommender','citation_batch') |
| `paper_user_state.state` | NEW — migration 047 | ENUM ('inbox','to_read','reading','done','trash') replaces 5 booleans + status |
| `paper_user_state.state_before_trash` | NEW — migration 047 | ENUM, NULLABLE, set on Trash for Restore |
| `recommendation_feedback` table | NEW — migration 049 | (paper_id, user_id, signal, source, topic_id, reason, created_at); UNIQUE NULLS NOT DISTINCT (paper_id, user_id, source) |
| `RejectedTopicsPanel.tsx` | NEW — frontend Settings | Lists dampened topics; reset button |
| `FeedbackButtons.tsx` | NEW — shared frontend component | Renders 👍/👎 conditionally based on origin |
| L1 negative_topics query | NEW — pulse/profile.py | Reads recommendation_feedback for stage-2 LLM context |
| L2 negative_centroid math | NEW — pulse/scoring.py | Cosine penalty term in stage-1 |
| L3 recommender exclusion | NEW — recommender.py | Excludes papers with negative feedback in last 60d |
| L3 topic dampening | NEW — pulse/profile.py | ≥5 negatives in 90d → topic-bonus halved |
| Hard-delete order (Amendment 1) | services/paper_ingestion/paper_ingestion/routers/papers.py:797-824 | TODAY: Qdrant-before-DB, both inside txn (audit NEW-H2 — data-loss risk). REDESIGN: SQL DELETE inside txn, Qdrant cleanup AFTER commit, best-effort. |
| `delete_paper_vectors` import | services/paper_ingestion/paper_ingestion/ingestion/embedder.py | Promoted to top-of-module in Phase A (resolves NI-4 deferred-import warning) |

---

## 15. Audit Findings — Disposition (from 2026-04-30 deep-audit)

The 2026-04-30 deep-audit ([docs/plans/2026-04-30-deep-audit-security-report.md](../plans/2026-04-30-deep-audit-security-report.md)) listed 11 still-open findings. **All 11 were closed by the WS-AH2 sprint** (commits `c6cfd14..6b8b83e` on master, merged 2026-04-30) — see [docs/known-residual-risks.md §"WS-AH / WS-AH2 — CLOSED"](../known-residual-risks.md). This spec inherits the codebase post-WS-AH2; **no audit findings remain open as Phase A entry conditions**.

The disposition table below records, for each WS-AH2 fix, whether Phase A **preserves** the fix (the underlying mechanic still exists post-redesign), **structurally supersedes** it (Phase A removes the underlying mechanic, making the WS-AH2 fix moot), or **inherits-as-is** (the fix is in shared code unaffected by Phase A).

| Audit ID | WS-AH2 commit | Disposition under Phase A |
|---|---|---|
| **NEW-H2** Hard-delete Qdrant order | `c6cfd14` | **PRESERVED.** Phase A spec §4.1 + §13.10 require the same order (SQL DELETE inside txn, Qdrant cleanup outside, best-effort). The endpoint moves from `DELETE /api/papers/{id}` (current shape, `/{id}/{trash}` precondition replaced with `state='trash'`); the ordering rule is unchanged. |
| **NEW-M8** `_assert_confirm_title_matches` trim | `c6cfd14` | **STRUCTURALLY SUPERSEDED.** Phase A §5.1 removes the title-type confirmation modal entirely. The helper function and `confirm_title` request-body field are deleted in Phase A. The WS-AH2 trim fix becomes moot (no comparison to do). |
| **DRY-1** `IS_ARCHIVED_SQL` predicate substitution | `c6cfd14`, `6e82103`, `75878de` | **STRUCTURALLY SUPERSEDED.** Phase A migration 047 drops the `archived` column. `IS_ARCHIVED_SQL` is replaced by state-based view predicates (§6). The 9 substituted call sites are rewritten to `pus.state IN (…)` form during Phase A. WS-AH2's centralization work is preserved as a *pattern* (predicates remain centralized in `predicates.py`); the specific constant changes shape. |
| **NI-1** `pulse_decks` INSERT user_id column | `6e82103` | **PRESERVED.** Phase A's `_persist_deck_inner` rewrite touches the same INSERT. Column list MUST keep `user_id` (audit-validated multi-tenant correctness). |
| **NI-2 / M11** Telegram digest user_id scoping | `6785cc8` | **PRESERVED.** Phase A spec §5.3 Telegram parity row maintains the `_simple_digest(db_user_id: int \| None)` parameter and `IS NOT DISTINCT FROM` binding pattern. |
| **NI-3** HardDeleteModal mutation `onError` | `bc66ddb` | **PRESERVED.** Phase A §9.4 codifies "every mutation has `onError` toast" as a uniform frontend rule across all components. |
| **H5** Bulk clear on URL-driven surface change | `ef7b3e0` | **PRESERVED.** Phase A §9.4 keeps `useEffect(() => useBulkSelection.getState().clear(), [surface])` as the only correct mechanism (chip-handler-only clearing was insufficient). |
| **NI-5** `app_factory` equal-length init/teardown | `a65ede4` | **INHERITED-AS-IS.** Lives in `libs/jarvis_common/jarvis_common/app_factory.py` — unaffected by Phase A. The contract is documented in [docs/ENGINEERING_STANDARDS.md §API](../ENGINEERING_STANDARDS.md). |
| **NI-6** Migration-lint script anchor | `6b8b83e` | **INHERITED-AS-IS.** Lives in `scripts/check-migrations-no-tx.sh` — unaffected by Phase A. |
| **L12** Legacy `'starred'` status warning | `6785cc8` | **STRUCTURALLY SUPERSEDED.** Phase A migration 047 drops the `status` column entirely. The legacy enum value cannot exist post-migration. |
| **NI-4** `delete_paper_vectors` deferred import | (folded into `c6cfd14`) | **PRESERVED.** Phase A keeps the top-of-module import — Spec §14 Verified Identifiers row pins this. |

**Summary:** 6 PRESERVED, 3 STRUCTURALLY SUPERSEDED, 2 INHERITED-AS-IS. Phase A entry conditions are clean — no open findings, no in-flight fixes to coordinate with.

**Reading the audit report today:** the 2026-04-30 deep-audit document remains in the repo as a historical record. Treat it as a snapshot of pre-WS-AH2 state, not a current TODO. The authoritative "what's still open" is [docs/known-residual-risks.md](../known-residual-risks.md).

---

## 16. Library Choices and Follow-Up Sprints

This redesign is intentionally **library-friendly** — Phase B sprints layer on top with no schema or contract changes.

### What this spec uses (verified live, 2026-04-30)
| Concern | Library | Status |
|---|---|---|
| LLM gateway | LiteLLM | Already in use |
| Embeddings | `nomic-embed-text` via Ollama | Already in use; upgrade candidates flagged in Phase C |
| Cross-encoder reranker | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Replaced by `mxbai-rerank-base-v2` in Phase B.3 |
| BM25 | `rank_bm25` | Already in use |
| FSRS scheduler | `py-fsrs` | Already in use |
| Validation | Pydantic v2 | Already in use |
| Backend | FastAPI | Already in use |
| Frontend icons | lucide-react | Already in use (see §5.1 mapping) |

### What Phase B sprints will add (deferred to dedicated specs)
| Phase | Library | What it replaces | Compatibility note |
|---|---|---|---|
| **B.1** | [Instructor](https://python.useinstructor.com/) | Manual `json.loads()` after LLM calls (29 sites today) | Built on Pydantic; works with Ollama via LiteLLM. Needs one-time integration test. The Pydantic models for §7 stage-2 reranker output are designed to drop into Instructor with no rewriting. |
| **B.2** | [Langfuse](https://langfuse.com/) | Zero observability today | LiteLLM-native integration. Self-hostable Docker. Every endpoint in §8 is a clean `@observe()` trace boundary. The L1+L2+L3 feedback loop becomes inspectable in the Langfuse dashboard. |
| **B.3** | [`mxbai-rerank-base-v2`](https://huggingface.co/mixedbread-ai) (Apache 2.0) | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Drop-in via sentence-transformers (same API). 3-stage RL training (GRPO + Contrastive + Preference Learning) outperforms Cohere/Voyage on BEIR. |
| **B.4** | [Taskiq](https://github.com/taskiq-python/taskiq) | Custom 726-line `libs/jarvis_common/jarvis_common/jobs.py` (PG+LISTEN/NOTIFY) | High-risk refactor (broker decision: Redis vs `arq`/`procrastinate`). The lifecycle endpoints in §8 don't enqueue any jobs themselves, so Phase A is unaffected by B.4's broker choice. |

### What this spec deliberately does NOT use
| Library | Why not |
|---|---|
| BERTopic | Auto-topic clustering — current taxonomy is user-defined (§10 OOS). Right choice when topic auto-discovery becomes in scope. |
| Pydantic AI | Agent framework — JARVIS isn't agent-based today. Right choice IF Hermes Phase 4 (agentic briefing) returns to scope. |
| LangGraph | Same — agent state machines. JARVIS pipelines are too direct. |
| DSPy | Programmatic prompt optimization — needs a benchmark dataset we don't have. |
| Outlines / LMQL | Constrained generation — Instructor is sufficient for our schemas. |
| recbole / lightfm / implicit | Recommender frameworks — designed for collaborative filtering at scale. Single-tenant + Rocchio-style centroid math is the right level of complexity for Phase A's L1+L2+L3. |
| HuggingFace `microsoft/recommenders` | Research benchmarking framework, not production drop-in. |
| `sentence-transformers` `mine_hard_negatives()` | Useful only if we fine-tune embeddings per-user — explicitly OOS in §10. |

**Verdict:** Phase A relies entirely on libraries already in the codebase. Phase B brings in 4 new dependencies, each with its own dedicated spec. Phase C is a model swap (no new library, just a new HF model). Phase D is open-ended.
