<!-- verified-against-UI: 2026-05-18 | routes: /settings, /settings?section=&item= -->

# Settings

_This area is evolving; verified 2026-05-18._

The **Settings** page at `/settings` is a two-pane interface: a **SettingsRail** on the left lists the sections and items, and the right panel shows the detail form for the currently selected item. The active section and item are reflected in the URL query parameters (`?section=&item=`).

Settings are organised into six sections. Access to each section or item depends on your role (**Admin** or regular **User**). Items marked **ADMIN** are only editable by users with the Admin role.

> **Role enforcement:** If you are a non-admin user and you follow a deep-link to an admin-gated section or item, the application silently redirects you to **Research → Topics** (the first non-admin section). No error is shown.

---

## §I — Account

Available to **all users**.

### Profile

Edit your display name and email address. Changing your email triggers a verification flow to confirm the new address before it takes effect.

### Appearance

Toggle between **light mode**, **dark mode**, and **system** (follows your OS preference).

---

## §II — Sources

**ADMIN only.**

### Sources

Enable and configure the paper data sources the system uses to discover new papers: arXiv, Semantic Scholar, OpenAlex, PubMed, and any configured custom sources. Each source can be individually toggled on or off, and source-specific API keys and parameters are entered here.

---

## §III — Models

**ADMIN only.**

### LLM Models

Configure which model aliases (**smart**, **fast**, **embed**) are mapped to which local or cloud models. The smart model is used for summarisation and reasoning; the fast model is used for lower-latency tasks; the embed model is used for generating embeddings.

### Providers

Configure API keys and endpoints for cloud LLM providers (OpenAI, Anthropic, Gemini) and LiteLLM routing. Changes here affect which models are available for the LLM configuration item above.

### AI Backend

Select the inference backend and model used for LLM calls. The panel shows:

- **Hardware Tier** — the automatically detected GPU/CPU tier for this machine, with a **Re-detect** button to refresh. If a GPU was present at install but the stack is running on CPU (overlay not engaged), an amber banner here tells you to re-run `setup.sh`.
- **Current Status** — the configured backend/model, the recently observed backend, and the recommended model for the detected hardware tier.
- **Backend selector** — toggle between **ollama** (local, the recommended default on every tier) and **vllm** (advanced, high-throughput GPU). vLLM is a manual overlay that is not auto-started — see "GPU acceleration" in the deployment guide; the recommended **model** still scales with the detected tier.
- **Model dropdown** — choose from evaluated candidates for the selected backend and tier. A score and brief reasoning is shown for each candidate. Applying a not-yet-pulled Ollama model pulls it first, so the backend never routes to a missing model.
- **Apply / Reset** — save the selection or revert to the last saved state.

---

## §IV — System

**ADMIN only.**

### Automation

Configure the schedule for automatic background jobs: when to fetch new papers from sources, when to run Pulse deck generation, and when to run other scheduled maintenance tasks.

### Extraction Templates

Create and manage templates used on the [Extraction Table](extraction-table.md) page. Each template defines a set of fields to extract from papers. Templates can be named, edited, and deleted here.

### Email / SMTP

Configure the outbound email relay for magic-link sign-in emails. Fields: SMTP host, port, username, password, sender address. A test-send button is available.

### Pulse

Configure Pulse-specific settings. The panel is divided into two cards:

- **Schedule card** — toggle Pulse on/off, set the daily run time, and adjust the **deck size** (5–30 papers; slider), ranking candidates, lookback window, and startup grace period.
- **Advanced tuning card** (collapsible) — **signal-weight sliders** for each scoring signal (topic relevance, recency, citation count, etc.), discovery seed balance (liked-papers weight and project-context weight), and L2 negative-feedback penalty. Weight presets are available for common configurations.

### Timer

Configure the Pomodoro-style session timer available in the TopBar: work interval, break interval, and long-break interval.

### Observability

Configure the Langfuse observability integration for tracing LLM calls. This setting is **hardware- and opt-in gated** — it requires a running Langfuse instance and is only active when explicitly enabled.

_This area is evolving; verified 2026-05-18._

### Access Mode

Control whether the instance allows additional user accounts beyond the admin account.

- **Single-user** — only the admin account can log in.
- **Multi-user** — additional accounts can be invited via magic-link (see [Admin & Multi-tenant](admin.md)).

Changing the access mode requires an application restart by an administrator.

---

## §V — Integrations

### Telegram

Available to **all users**. Pair your personal account with the configured Telegram bot to receive Pulse digests and interact with your library from Telegram. See [Telegram](telegram.md) for the full pairing flow.

### Bot Token

**ADMIN only.** Configure the Telegram bot token that the system uses to send messages. This is a server-level setting; each user pairs to the bot configured here. See [Telegram](telegram.md) for more context.

### Zotero

Available to **all users**. Connect your Zotero account to enable Zotero sync from the [Paper Detail](paper-detail.md) page and the ZoteroPanel.

---

## §VI — Research

Available to **all users**.

### Topics

Create and manage your research topics. Topics are used by the Pulse engine to select relevant papers for your daily deck. Each topic has a name and a description; more specific descriptions improve recommendation quality.

### Authors

Track specific authors. Papers by tracked authors are surfaced in your Pulse deck and feed.

### Spaced Repetition

Configure FSRS parameters for the [Learning Cards](learning-cards.md) system: desired retention rate and learning step intervals.

---

## Related pages

- [Getting Started](getting-started.md) — the onboarding wizard pre-configures Sources, Topics, and Automation before you arrive here.
- [Telegram](telegram.md) — step-by-step pairing guide (Integrations → Telegram).
- [Admin & Multi-tenant](admin.md) — admin-only pages for user management, audit log, and system health.
- [Extraction Table](extraction-table.md) — uses templates managed in §IV System → Extraction Templates.
- [Learning Cards](learning-cards.md) — uses FSRS parameters from §VI Research → Spaced Repetition.
