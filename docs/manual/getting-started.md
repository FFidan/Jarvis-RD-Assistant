<!-- verified-against-UI: 2026-06-06 | routes: /onboarding (wizard), /auth/verify -->

# Getting Started

This page walks through everything needed to go from a fresh installation to a working research session: operator-level bootstrap via the onboarding wizard, signing in, and first-use setup. Steps marked **operator** require administrative access to the server; steps marked **user** apply to everyone after the operator has bootstrapped the system.

---

## Installation (operator)

JARVIS RD Assistant runs as a set of Docker Compose services. Full installation instructions — Docker prerequisites, environment variables, TLS configuration, and first-boot checklist — are in the repository's **[DEPLOYMENT.md](../DEPLOYMENT.md)**. This manual does not duplicate those steps.

---

## Onboarding wizard (operator & user)

On a fresh installation — or whenever `setup_completed` is not yet `true` — the application replaces the normal UI with the **Onboarding Wizard**. This single continuous flow spans the full bootstrap: it starts before any account exists and ends after the admin is signed in and the instance is configured. Once the wizard is complete it does not appear again.

<!-- screenshot: Onboarding wizard — Step 1 System Check, showing status pills for Postgres, Qdrant, Ollama, and LiteLLM -->

The wizard gate is driven by the pre-auth `/api/setup/status` endpoint (`setup_completed` field). Because the check does not require an existing session, the same gate covers both a fresh install (no admin yet) and a partially-completed setup where the admin exists but has not yet finished the post-auth steps.

The wizard has nine steps (the admin-create step is conditionally skipped when an admin already exists, e.g. when resuming after a page reload or when the admin was created via the CLI):

### Step 1 — Welcome & system check

The wizard probes four backend services: **Postgres**, **Qdrant**, **Ollama**, and **LiteLLM**. Each probe shows a status indicator.

The models-ready check reports green when **both** of the following are true:

- The embedder is present (any model whose name starts with the configured embedding model prefix, e.g. `qwen3-embedding`).
- At least one qwen3 chat model is present (`qwen3:4b`, `qwen3:8b`, or `qwen3:14b`).

The default install (`setup.sh`) pulls `qwen3:8b` and `qwen3-embedding:4b`, which satisfies the ready condition. If models are still downloading, the check shows "still pulling" rather than a generic error.

If any service is unreachable, fix it in your Docker Compose environment and click **Re-check** before proceeding.

### Step 2 — SMTP relay (skippable)

Configure the outbound email relay that JARVIS uses to send magic-link sign-in emails. Fields: SMTP host, port, username, password, sender (From) address, and a test recipient address. Two additional optional fields are available:

- **Reply-To address** — when set, email clients route replies here instead of the From address. Leave blank to omit.
- **Sender display name** — when set, the From header shows a friendly name, e.g. `JARVIS RD <login@your-domain.dev>`. Leave blank to omit.

Use the **Save & test send** button to save the settings and verify delivery in one step before continuing. During the wizard (before any admin account exists), the test email is always sent to the From address — an admin can only mail themselves at this stage. This step is skippable — you can configure email later in Settings → System → Email / SMTP if the instance will use API-key login only.

### Step 3 — Create admin & sign in

Enter the email address for the first administrator account and click **Create admin & sign in**. The system creates the account and establishes a session in the same step (no separate magic-link round-trip needed). This is the mid-flow auth boundary: steps 4–9 require an active session and run after this point.

This step is skipped when an admin already exists (for example, when resuming a partially-completed setup after the admin was already created).

### Step 4 — Cloud LLM keys (skippable)

Optionally provide cloud provider API keys. Cloud models can supplement the local Ollama models when an administrator enables them, but this step is skippable and local-only installs remain supported. More providers and custom OpenAI-compatible endpoints can be added later in Settings → Models → Cloud Providers.

### Step 5 — First research topic

Enter a **name** and **description** for your first research topic. Topics drive Pulse recommendations and scoped library searches. You can add more topics later in Settings → Topics.

### Step 6 — Automation schedule

Configure how often JARVIS automatically generates Pulse recommendation decks.

- Toggle **Pulse enabled** on or off.
- Pick a **daily run time** using the time picker.
- A cron expression preview shows the resulting schedule.

The schedule can be changed at any time in Settings → Automation.

### Step 7 — Source API keys (skippable)

Enter API keys for the research data sources that support them — **Semantic Scholar**, **OpenAlex**, and **PubMed**. (arXiv needs no key, and Zotero is connected separately later under Settings → Integrations → Zotero.) Adding keys raises rate limits for paper discovery; sources still work without them. This step is skippable; keys can be configured later in Settings → Sources.

### Step 8 — Pair Telegram (skippable)

Pair your account with the Telegram bot to receive Pulse digests and send queries from Telegram. Follow the on-screen instructions to obtain a pairing code, then send `/pair <code>` to the bot. This step is skippable; pairing can be completed later in Settings → Integrations → Telegram.

### Step 9 — You’re all set

Setup is complete. `setup_completed` is set to `true`. The wizard does not appear again. The page redirects to `/` (the Home page).

---

## Signing in — `/auth/verify` (user)

<!-- screenshot: LoginPage showing the email input and "Send magic link" button -->

**Signing in** depends on how the server is configured.

- **If SMTP is configured** (your admin set up an email relay in Settings → System → Email / SMTP), the login page defaults to the magic-link tab: enter your email address, click **Send magic link**, and click the link in the email you receive. The link is single-use and expires after a short window; return to the login page and request a new one if it has expired.
- **Single-user mode without SMTP:** the login page defaults to the API-key tab. Enter the `JARVIS_API_KEY` value set in your server's environment and click **Sign In**.
- **Multi-user mode without SMTP:** the login page stays on the magic-link tab with a notice that links cannot be delivered. Ask your admin to configure SMTP in Settings → System → Email / SMTP. The API-key tab is still reachable, but the backend rejects API-key sign-in once more than one account exists — unless the operator has explicitly set `API_KEY_LOGIN_ENABLED=true`.

---

## Onboarding tour (user)

<!-- screenshot: Onboarding tour overlay — step 1 pointing at Settings → Sources in the sidebar -->

After completing setup, a **guided onboarding tour** starts automatically if your account has no research topics and no papers and you have not previously dismissed the tour. The tour is powered by react-joyride and has four steps:

1. **Settings → Sources** — pointing to where you enable and configure paper sources.
2. **Settings → Topics** — pointing to where you create and manage research topics.
3. **Pulse generate button** — showing how to trigger a Pulse recommendation deck manually.
4. **Pulse card rating** — demonstrating how to rate a recommendation card.

You can dismiss the tour at any step. It does not repeat once dismissed.

---

## Your first paper (user)

Once you are signed in and the onboarding wizard is complete, here is the fastest path to a working research session.

### Save a paper

Open **Discover** from the sidebar to browse the Pulse recommendation deck, or go to **Library → Add paper** and paste an arXiv URL, DOI, or title. Saving a paper adds it to your library with metadata (title, authors, abstract) but does not yet extract findings.

> **Single-user mode note:** if you are the only user on this instance, do not expose the dashboard to the open internet without first reviewing the access mode and authentication settings. Single-user installs with API-key login are designed for loopback or VPN access.

### Analyze the paper

Click the paper card in your Library and then click **Analyze**. JARVIS runs the full analysis pipeline: it fetches the PDF, chunks and embeds the text, extracts findings with exact-quote backing, generates flashcards, and builds the knowledge graph for that paper.

On a GPU this typically takes a few minutes. On CPU-only hardware it can take 30 minutes or more — the progress indicator in the paper detail view shows each stage as it completes.

### What you see after analysis

- **Summary tab** — a verified summary of the paper's main contributions, with each claim linked to a source quote and page number.
- **Cards tab** — FSRS flashcards generated from the paper's key findings. Open **Learning → Cards** to start a review session.
- **Ask** — once your first paper is analyzed you can ask questions about it (and any other analyzed papers in your library). Open **Ask** from the sidebar, type a question, and JARVIS retrieves relevant passages and synthesises a cited answer.

The **Knowledge Graph** in the sidebar shows entities and relationships extracted from your analyzed papers.

---

## What comes next

Continue to [Navigation](navigation.md) for a tour of the AppShell and sidebar, or jump directly to [Research Feed & Library](research-feed.md) to start adding more papers.
