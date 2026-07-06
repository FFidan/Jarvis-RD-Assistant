<!-- verified-against-UI: 2026-07-05 | routes: /settings, /settings?section=&item= -->

# Settings

_This area is evolving; verified 2026-06-06._

The **Settings** page at `/settings` is a two-pane interface: a **SettingsRail** on the left lists the sections and items, and the right panel shows the detail form for the currently selected item. The active section and item are reflected in the URL query parameters (`?section=&item=`).

Settings are organised into six sections. Access to each section or item depends on your role (**Admin** or regular **User**). Items marked **ADMIN** are only editable by users with the Admin role.

> **Role enforcement:** If you are a non-admin user and you follow a deep-link to an admin-gated section or item, the application silently redirects you to **Research → Topics** (the first non-admin section). No error is shown.

---

## §I — Account

Available to **all users**.

### Profile & Email

Edit your display name and email address. Changing your email triggers a verification flow to confirm the new address before it takes effect.

### Account data export

Download a ZIP of your own account data from the Account section. The export is scoped to the signed-in user and includes structured account records; it is not a shared corpus export or a PDF backup.

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

Choose the models JARVIS uses. Each control is labelled in plain language with its technical alias in parentheses, so you don't need to know the jargon:

- **Main model (smart)** — writes your summaries, cards, and Ask answers.
- **Quick model (fast)** — scores and triages papers.
- **Embedding model (embed)** — powers search; it is fixed, and changing it requires re-indexing your library.
- **Reading window (num_ctx)** — how much of each paper the AI reads at once.

Your choice applies automatically — there is no separate "save and restart" step. Operator-level tuning knobs (such as the reading window and the thinking toggle) sit behind a per-model **Configure** disclosure so the everyday controls stay uncluttered.

**First-run pick banner.** After setup completes, a green banner shows the model JARVIS selected for your GPU — for example, "We picked qwen3:8b for your 15.9 GB GPU — change anytime in Settings → Models." This is just a confirmation; use the dropdowns below to change the selection at any time.

**Advisory recommendation banner.** Below the first-run confirmation, an advisory banner lists the per-alias recommendations for your hardware tier. This is informational only — it does not change your active model automatically.

**Hardware source line.** JARVIS shows a short detail line below the hardware strip explaining how VRAM was detected — for example, "GPU detected inside the container" (or "GPU detected on the host at install time", "estimated from Apple Silicon unified memory", or "no GPU detected — running on CPU").

**GPU overlay divergence warning.** If an amber warning reads "{N} GB detected on host — GPU overlay not active", JARVIS detected GPU VRAM from the host but the Docker GPU overlay is not active for this container. Re-run `setup.sh` with GPU support enabled to activate the overlay and use the detected VRAM for model inference.

If a model card shows a **pending — applying automatically** badge, your choice was saved but the model service is temporarily unavailable (for example, it is still starting up or its database is unreachable). JARVIS keeps your selection and retries delivery automatically — roughly every 30 seconds — and applies it the moment the model service recovers, with no restart or further action needed from you. In the meantime, summaries and answers continue to use the previously active model, so nothing silently breaks. A badge that persists for many minutes means the model service is stuck unhealthy; check the Health indicators.

### Providers & Routing

Configure optional cloud LLM providers for this deployment. The panel keeps connected providers visible and uses **Add cloud provider** for additional choices, so administrators do not have to manage a long wall of empty API-key inputs. Supported provider entries include OpenAI, Anthropic, Google Gemini, OpenRouter, DeepSeek, Mistral, Kimi/Moonshot, Z.ai/GLM, and a Custom OpenAI-compatible endpoint.

Provider settings are admin-wide: changes affect the instance, not only the signed-in administrator. Keys are stored encrypted at rest, shown only as configured/not configured, and blank saves do not delete an existing key. Custom OpenAI-compatible endpoints require an explicit base URL and are intended for trusted self-hosted or institutional gateways.

Adding a key does not make cloud the default. It only makes matching cloud models assignable in the **Main model (smart)** and **Quick model (fast)** controls above. Leave all provider keys blank to keep the deployment local-only.

### AI Backend

Select the inference backend and model used for LLM calls. The panel shows:

- **Hardware Tier** — the automatically detected GPU/CPU tier for this machine, with a **Re-detect** button to refresh. If a GPU was present at install but the stack is running on CPU (overlay not engaged), an amber banner here tells you to re-run `setup.sh`.
- **Current Status** — the configured backend/model, the recently observed backend, and the recommended model for the detected hardware tier.
- **Backend selector** — toggle between **ollama** (local, the recommended default) and **vllm** (advanced, high-throughput GPU). The recommended **model** scales with the detected hardware tier.
- **Model dropdown** — choose from evaluated candidates for the selected backend and tier. A score and brief reasoning is shown for each candidate. Applying a not-yet-pulled Ollama model pulls it first, so the backend never routes to a missing model. Cloud providers are configured separately and are optional role assignments, not backend defaults.
- **Apply / Reset** — save the selection or revert to the last saved state.

---

## §IV — System

**ADMIN only.**

### Automation

Configure the schedule for automatic background jobs: when to fetch new papers from sources, when to run Pulse deck generation, and when to run other scheduled maintenance tasks.

### Extraction Templates

Create and manage templates used on the [Extraction Table](extraction-table.md) page. Each template defines a set of fields to extract from papers. Templates can be named, edited, and deleted here.

### Email / SMTP

Configure the outbound email relay for magic-link sign-in emails. Fields: SMTP host, port, username, password, and sender (From) address. Two optional fields are also available:

- **Reply-To address** — when set, email clients route replies here instead of the From address. Leave blank to clear.
- **Sender display name** — when set, the From header shows a friendly name, e.g. `JARVIS RD <login@your-domain.dev>`. Leave blank to clear.

**Misconfiguration warning.** If the saved SMTP configuration is not deliverable — for example, a host is set but the From address is missing, a required field was saved as an empty string, or no relay is configured at all — the card displays an amber warning banner describing the issue. The form remains editable so you can correct the problem in place without navigating away.

**Save & send test email.** In addition to the **Save** button, a **Save & send test email** button saves the settings and immediately attempts a test delivery. An optional **Test recipient** field (defaults to the From address) lets you direct the test message to a specific address. The result — success or the exact SMTP error — is shown inline. This matches the test-send available during the onboarding wizard.

### Pulse

Configure Pulse-specific settings. The panel is divided into two cards:

- **Schedule card** — toggle Pulse on/off, set the daily run time, and adjust the **deck size** (5–30 papers; slider), ranking candidates, lookback window, and startup grace period.
- **Advanced tuning card** (collapsible) — fine-tune how candidates are ranked: signal-weight sliders for relevance, recency, and citation count, plus discovery balance and negative-feedback controls. Weight presets cover common configurations. Includes a **Recommendations enabled** toggle that controls whether personalised paper recommendations are computed at all.

### Timer

Configure the Pomodoro-style session timer available in the TopBar: work interval, break interval, and long-break interval.

### Observability

Configure the Langfuse observability integration for tracing LLM calls. This setting is **hardware- and opt-in gated** — it requires a running Langfuse instance and is only active when explicitly enabled.

_This area is evolving; verified 2026-06-06._

### Sign-in Method

Control how people sign in to this instance.

- **Single-user** — only the admin account can log in (API-key login; no email/SMTP required).
- **Multi-user** — additional accounts can be invited via magic-link email (see [Admin & Multi-tenant](admin.md)).

The change applies on the next status check — no restart required.

---

## §V — Integrations

### Telegram

Available to **all users**. Pair your personal account with the configured Telegram bot to receive Pulse digests and interact with your library from Telegram. See [Telegram](telegram.md) for the full pairing flow.

### Bot Token

**ADMIN only.** Configure the Telegram bot token that the system uses to send messages. This is a server-level setting; each user pairs to the bot configured here. See [Telegram](telegram.md) for more context.

### Zotero

Available to **all users**. Connect your Zotero account to push papers and copy citation keys from the [Paper Detail](paper-detail.md) page.

Fill in:

- **API Key** and **User ID** — both are on [zotero.org/settings/keys](https://www.zotero.org/settings/keys). Create the key with read/write library access.
- **Library Type** — choose one:
  - **Personal library** — your own Zotero account. Most people want this.
  - **Group library** — a shared Zotero group. You will also need the group's **numeric Group ID** (the number in the group URL, e.g. `zotero.org/groups/987654/...`).

Sending a paper to Zotero pushes its **citation metadata** (title, authors, DOI, abstract). The PDF file itself is **not attached**.

#### Verify Zotero works

1. Enter your **API Key** and **User ID** (and **Group ID** for a group library).
2. Click **Test connection**. A green "Connected" message confirms the credentials are valid; a red message names the problem (wrong key, missing user ID, or unreachable Group ID).
3. Open any paper's [Paper Detail](paper-detail.md) page and click **Send to Zotero**. The item then appears in your Zotero library, and the panel offers a **View in Zotero** link.

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
