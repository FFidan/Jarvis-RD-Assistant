<!-- verified-against-UI: 2026-07-20 | routes: /settings, /settings?section=&item= -->

# Settings

The **Settings** page at `/settings` is a two-pane interface: a **SettingsRail** on the left lists the sections and items, and the right panel shows the detail form for the currently selected item. The active section and item are reflected in the URL query parameters (`?section=&item=`).

Settings are organised into six sections. Access to each section or item depends on your role (**Admin** or regular **User**). Items marked **ADMIN** are only editable by users with the Admin role.

> **Role enforcement:** If you are a non-admin user and you follow a deep-link to an admin-gated section or item, the application silently redirects you to **Research → Topics** (the first non-admin section). No error is shown.

---

## §I — Account

Available to **all users**.

### Profile & Email

Edit your display name and email address. Changing your email triggers a verification flow to confirm the new address before it takes effect.

### Account data export

Download a ZIP of your own account data from the Account section. This is the GDPR/account export: it is scoped to the signed-in user and includes structured account records, not an instance-wide paper export or a PDF backup.

### Appearance

Toggle between **light mode**, **dark mode**, and **system** (follows your OS preference).

### Passkeys

Register **passkeys** (fingerprint, Face ID, or a hardware security key) for password-less sign-in, and view or revoke the passkeys registered to your account. Registration requires user verification and works only on the exact origin the instance is served from (HTTPS for any non-localhost address), so the controls appear only where passkeys can actually work. See [Passkeys](passkeys.md) for the full guide, including magic-link recovery if you lose a device.

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
- **Embedding model (embed)** — powers search and is dimension-locked to its
  Qdrant collection. It is not changed with the Main or Quick picker; follow
  [Changing the embedding model](changing-embedding-model.md) for the required
  backup, benchmark, and re-embedding workflow.
- **Reading window (num_ctx)** — how much of each paper the AI reads at once.

Your choice applies automatically — there is no separate "save and restart" step. Operator-level tuning knobs (such as the reading window and the thinking toggle) sit behind a per-model **Configure** disclosure so the everyday controls stay uncluttered.

**First-run pick banner.** After setup completes, a green banner shows the tier-selected model JARVIS chose for your GPU — for example, "We picked qwen2.5:7b-instruct for your 15.9 GB GPU — change anytime in Settings → Models." This is just a confirmation; use the dropdowns below to change the selection at any time.

**Advisory recommendation banner.** Below the first-run confirmation, an advisory banner lists the per-alias recommendations for your hardware tier. This is informational only — it does not change your active model automatically.

**Hardware source line.** JARVIS shows a short detail line below the hardware strip explaining how VRAM was detected — for example, "GPU detected inside the container" (or "GPU detected on the host at install time", "estimated from Apple Silicon unified memory", or "no GPU detected — running on CPU").

**GPU overlay divergence warning.** If an amber warning reads "{N} GB detected on host — GPU overlay not active", JARVIS detected GPU VRAM from the host but the Docker GPU overlay is not active for this container. Re-run `setup.sh` with GPU support enabled to activate the overlay and use the detected VRAM for model inference.

If a model card shows a **pending — applying automatically** badge, your choice was saved but the model service is temporarily unavailable (for example, it is still starting up or its database is unreachable). JARVIS keeps your selection and retries delivery automatically — roughly every 30 seconds — and applies it the moment the model service recovers, with no restart or further action needed from you. In the meantime, summaries and answers continue to use the previously active model, so nothing silently breaks. A badge that persists for many minutes means the model service is stuck unhealthy; check the Health indicators.

### Providers & Routing

Configure optional cloud LLM providers for this deployment. The panel keeps connected providers visible and uses **Add cloud provider** for additional choices, so administrators do not have to manage a long wall of empty API-key inputs. Supported provider entries include OpenAI, Anthropic, Google Gemini, OpenRouter, DeepSeek, Mistral, Kimi/Moonshot, Z.ai/GLM, and a Custom OpenAI-compatible endpoint.

Provider settings are deployment-wide: changes affect the instance, not only the signed-in administrator. Keys are stored encrypted at rest, shown only as configured/not configured, and blank saves do not delete an existing key. Custom OpenAI-compatible endpoints require an explicit base URL and are intended for trusted self-hosted or institutional gateways.

Adding a key does not make cloud the default. It only makes matching cloud models assignable in the **Main model (smart)** and **Quick model (fast)** controls above. Leave all provider keys blank to keep the deployment local-only.

### Advanced: backend & hardware diagnostics

The **AI models** page is the authoritative place to assign the **Main model (smart)** and **Quick model (fast)** roles. The advanced diagnostics panel below those role cards is read-only for routing: it explains the current hardware/runtime state and points you back to the role cards when a model assignment should change.

The panel shows:

- **Hardware Tier** — the automatically detected GPU/CPU tier for this machine, with a **Re-detect** button to refresh. If a GPU was present at install but the stack is running on CPU (overlay not engaged), an amber banner here tells you to re-run `setup.sh`.
- **Current Status** — the configured backend/model, the recently observed backend, and the recommended model for the detected hardware tier.
- **Backend guidance** — a short explanation of **Ollama** as the default local runtime and **vLLM** as an optional high-throughput runtime when you already operate it behind the local LiteLLM route.
- **Model routing** — a reminder to select role models in the cards above, plus evidence labels for candidates when benchmark or catalog metadata is available.

Cloud providers are configured separately in **Providers & Routing**. Adding a cloud key makes matching cloud models assignable in the role cards; it does not change the deployment away from local-first operation by itself.

---

## §IV — System

**ADMIN only.**

### Automation

Configure the schedule for automatic background jobs: when to fetch new papers from sources, when to run Pulse deck generation, and when to run other scheduled maintenance tasks. `automation.auto_summarize_discovered` controls whether newly discovered papers are summarized automatically; it is off by default. This is a deployment-wide setting managed by an administrator, while the library papers and their resulting work remain scoped to the relevant library holder.

### Extraction Templates

Create and manage templates used on the [Extraction Table](extraction-table.md) page. Each template defines a set of fields to extract from papers. Templates can be named, edited, and deleted here.

### Email / SMTP

Configure the outbound email relay for magic-link sign-in emails. SMTP settings
are deployment-wide. Settings and the first-run wizard persist system-level
database rows; the password is encrypted under `JARVIS_CONFIG_KEY`, is never
returned to the browser, and takes effect without a service restart. Fields:
SMTP host, port, username, password, and sender (From) address. Two optional
fields are also available:

- **Reply-To address** — when set, email clients route replies here instead of the From address. Leave blank to clear.
- **Sender display name** — when set, the From header shows a friendly name, e.g. `JARVIS RD <login@your-domain.dev>`. Leave blank to clear.

**Misconfiguration warning.** If the saved SMTP configuration is not deliverable — for example, a host is set but the From address is missing, a required field was saved as an empty string, or no relay is configured at all — the card displays an amber warning banner describing the issue. The form remains editable so you can correct the problem in place without navigating away.

**Save & send test email.** In addition to the **Save** button, a **Save & send test email** button saves the settings and immediately attempts a test delivery. An optional **Test recipient** field (defaults to the From address) lets you direct the test message to a specific address. The result — success or the exact SMTP error — is shown inline. This matches the test-send available during the onboarding wizard.

**When SMTP isn't configured or delivery fails.** No magic link is ever printed to server stdout. A request that can't be delivered — no relay configured, a `DEV_SMTP_LOG_ONLY` dev instance, an SMTP error, or a link that would point at a non-loopback private host — logs only a hashed, PII-free event to Logs Live; the link and its token are never logged anywhere. To hand a user a working link when email isn't an option, use **Admin → Users → Send link** — that returns the actual sign-in URL to you (never delivers a bearer link automatically to a route that failed) so you can pass it along by hand.

There are two supported configuration layers. The normal UI layer is the
encrypted deployment-wide database configuration above. For unattended host
setup, `setup.sh --smtp-pass-file <host-path>` copies the supplied value into
the `secrets/smtp_pass.txt` Docker secret; it is a fallback when a database
field is absent. The installer path must name a file on the host, not
`/run/secrets/...` inside a future container. Neither layer places the password
in `.env` or `docker inspect` output.

### Pulse

Configure Pulse-specific settings. The panel is divided into two cards:

- **Schedule card** — toggle Pulse on/off, set the daily run time, and adjust the **deck size** (5–30 papers; slider), ranking candidates, lookback window, and startup grace period.
- **Advanced tuning card** (collapsible) — fine-tune how candidates are ranked: signal-weight sliders for relevance, recency, and citation count, plus discovery balance and negative-feedback controls. Weight presets cover common configurations. Includes a **Recommendations enabled** toggle that controls whether personalised paper recommendations are computed at all.

Repeated negative feedback for a topic dampens its positive similarity contribution to future recommendations. It never increases a negative-similarity score.

### Timer

Configure the Pomodoro-style timer available in the TopBar: work interval,
break interval, and long-break interval. Those preferences and break cycles are
local to this browser. The active focus interval itself is stored per user, so
starting it from the Web interface or Telegram shows the same remaining session
in the other client. Pause, resume, stop, and completed-time accounting use that
shared server state.

### Observability

Configure the Langfuse observability integration for tracing LLM calls. This setting is **hardware- and opt-in gated** — it requires a running Langfuse instance and is only active when explicitly enabled.

### Sign-in Method

Choose which login method the sign-in screen offers first. This setting does not change tenancy, library scoping, or whether admins can invite users. Admin invites are available in either mode.

- **Single-user** — the sign-in screen offers API-key login first; email/SMTP is optional for a solo local install.
- **Multi-user** — the sign-in screen offers magic-link login first; configure and test SMTP before inviting other users so links can be delivered.

The change applies on the next status check — no restart required.

---

## §V — Integrations

### Telegram

Available to **all users**. Telegram pairing is personal: pair your own account
with the configured bot to receive Pulse digests and interact with your library.
See [Telegram](telegram.md) for the full pairing flow.

### Bot Token

**ADMIN only.** The bot token is deployment-wide and encrypted in the database;
each user's pairing remains separate. The `telegram` Compose profile must be
enabled for the bot service to run, and a saved replacement token takes effect
after that service is restarted. See [Telegram](telegram.md) for more context.

### Zotero

Available to **all users**. Zotero credentials are per-user and encrypted. Connect
your own personal or group library to push papers and copy citation keys from
the [Paper Detail](paper-detail.md) page. Zotero-imported papers remain private
to the connecting user's JARVIS library; a Zotero group does not make them
public to every JARVIS account. See [Source-aware paper
visibility](../SECURITY.md#source-aware-paper-visibility).

Fill in:

- **API Key** and **User ID** — both are on [zotero.org/settings/keys](https://www.zotero.org/settings/keys). Create the key with read/write library access.
- **Library Type** — choose one:
  - **Personal library** — your own Zotero account. Most people want this.
  - **Group library** — a shared Zotero group. You will also need the group's **numeric Group ID** (the number in the group URL, e.g. `zotero.org/groups/987654/...`).

Each JARVIS account uses one active Zotero library at a time. Changing the User
ID, Library Type, or Group ID disconnects the item and collection references
from the previous library. It does not remove local papers, projects, notes, or
analysis history. The next export creates or finds the corresponding objects in
the newly selected library.

Sending a paper to Zotero pushes its **citation metadata** (title, authors, DOI,
abstract). The PDF file itself is **not attached**. The item is filed into a
Zotero collection matching each JARVIS project linked to the paper. Existing
items found by DOI are filed into those collections instead of duplicated.

#### Verify Zotero works

1. Enter your **API Key** and **User ID** (and **Group ID** for a group library).
2. Click **Test connection**. A green "Connected" message confirms the credentials are valid; a red message names the problem (wrong key, missing user ID, or unreachable Group ID).
3. Link a paper to a project, open its [Paper Detail](paper-detail.md) page, and
   click **Send to Zotero**. The job indicator shows the export until it
   finishes. The item then appears in a collection named after the project, and
   the panel offers a **View in Zotero** link.

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
