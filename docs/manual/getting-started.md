<!-- verified-against-UI: 2026-05-18 | routes: /first-run, /auth/verify, /setup -->

# Getting Started

This page walks through everything needed to go from a fresh installation to a working research session: operator-level bootstrap, signing in, and the post-login setup wizard. Steps marked **operator** require administrative access to the server; steps marked **user** apply to everyone after the operator has bootstrapped the system.

---

## Installation (operator)

JARVIS RD Assistant runs as a set of Docker Compose services. Full installation instructions — Docker prerequisites, environment variables, TLS configuration, and first-boot checklist — are in the repository's **[DEPLOYMENT.md](../DEPLOYMENT.md)**. This manual does not duplicate those steps.

---

## First-run operator bootstrap — `/first-run` (operator)

On a brand-new installation, before any user account exists, the application serves the **First Run Setup** wizard at `/first-run`. This is a one-time flow; once an admin account is created the route redirects to the normal login page.

<!-- screenshot: /first-run step 1 — System Check, showing status pills for Postgres, Qdrant, Ollama, and LiteLLM -->

The wizard has five steps:

### Step 1 — System check

The wizard probes four backend services: **Postgres**, **Qdrant**, **Ollama**, and **LiteLLM**. Each probe shows a status indicator. If any service is unreachable, fix it in your Docker Compose environment and click **Re-check** before proceeding.

### Step 2 — SMTP relay (skippable)

Configure the outbound email relay that JARVIS uses to send magic-link sign-in emails. Fields: SMTP host, port, username, password, sender address, and a test recipient address. Use the **Send test email** button to verify delivery before continuing. This step is skippable — you can configure email later in Settings if the instance will start with only API-key login.

### Step 3 — Admin account

Enter the email address for the first administrator account and click **Create admin & sign in**. The system creates the account and sends a magic-link email to that address (or, if SMTP was skipped, provides an alternative sign-in method). This is the only step that cannot be skipped or revisited from this wizard.

### Step 4 — Cloud LLM keys (skippable)

Optionally provide API keys for **OpenAI**, **Anthropic**, and/or **Gemini**. These enable cloud-hosted language models alongside the local Ollama models. This step is skippable; keys can be added later in Settings.

### Step 5 — Done

The wizard is complete. The page auto-redirects to `/` (the Home page).

---

## Signing in — `/auth/verify` (user)

<!-- screenshot: LoginPage showing the email input and "Send sign-in link" button -->

### Magic-link login (primary)

1. Navigate to the JARVIS RD Assistant URL. If you are not signed in you will see the **Login** page.
2. Enter your email address and click **Send sign-in link**.
3. Open the email and click the one-shot link. It lands on `/auth/verify`, which creates your session and redirects you to the application.

The magic-link is single-use and expires after a short window. If it has expired, return to the login page and request a new one.

### API-key fallback

If SMTP is not configured, or if you prefer direct key-based access, click **Use API key instead** on the login page and enter your API key. This method does not require email delivery.

---

## Post-login setup wizard — `/setup` (user)

After signing in for the first time your session is in **setup mode** (`setup_completed = false`). The application redirects new accounts to the **Setup Wizard** at `/setup`. Returning users who dismissed or bypassed the wizard earlier will see a **Setup Banner** on the Home page instead (see below).

<!-- screenshot: /setup step 3 — "First research topic" form with name and description fields -->

The wizard has seven steps:

### Step 1 — Welcome

An introduction to JARVIS RD Assistant and an overview of what the wizard will configure.

### Step 2 — System check

A brief automated check confirming that the backend services are reachable from your session. The check polls until all services report ready.

### Step 3 — First research topic

Enter a **name** and **description** for your first research topic. Topics drive Pulse recommendations and scoped library searches. You can add more topics later in Settings → Topics.

### Step 4 — Automation schedule

Configure how often JARVIS automatically generates Pulse recommendation decks.

- Toggle **Pulse enabled** on or off.
- Pick a **daily run time** using the time picker.
- A cron expression preview shows the resulting schedule.

The schedule can be changed at any time in Settings → Automation.

### Step 5 — Source API keys

Enter API keys for the research data sources you want to use (for example Semantic Scholar, Zotero). Only sources with valid keys will fetch new papers. This step is operator-visible — keys entered here are stored in the server-side configuration.

### Step 6 — Telegram pairing (skippable)

Pair your account with a Telegram bot to receive Pulse digests and send queries from Telegram. Follow the on-screen instructions to obtain a pairing code. This step is skippable; pairing can be completed later in Settings → Integrations → Telegram.

### Step 7 — Done

Setup is complete. `setup_completed` is set to `true` for your account. The wizard does not appear again. The page redirects to `/` (the Home page).

---

## Setup Banner (user)

If the setup wizard was not completed, a **dismissible Setup Banner** appears at the top of the Home page. It links back to `/setup` so you can complete the remaining steps at any time. Once all wizard steps are done, or once you explicitly dismiss the banner, it no longer appears.

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

## What comes next

Once you are signed in and the wizard is complete, continue to [Navigation](navigation.md) for a tour of the AppShell and sidebar, or jump directly to [Research Feed & Library](research-feed.md) to start adding papers.
