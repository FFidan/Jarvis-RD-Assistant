<!-- verified-against-UI: 2026-05-18 | routes: /settings?section=integrations&item=telegram, /settings?section=integrations&item=bot-token -->

# Telegram

JARVIS RD Assistant integrates with Telegram so you can receive Pulse digests and stay connected to your library from a mobile device. Each user pairs their personal Telegram account individually; the Telegram bot itself is configured by an admin.

The bot is optional and runs only when the `telegram` Compose profile is
enabled. Saving a token does not start a profile that was omitted during setup;
an operator must enable the profile and start the service on the JARVIS host.

---

## Who can do what

| Action | Role required |
|--------|--------------|
| Pair your personal Telegram account | All users |
| Manage your pairing (re-pair, un-pair) | All users |
| Configure the bot token (server-level) | Admin only |

---

## Pairing your account — Settings → Integrations → Telegram

To connect your Telegram account:

1. Navigate to **Settings → Integrations → Telegram** (visible to all users).
2. The panel shows your current pairing status. If you are not yet paired, a **Generate pairing code** button is available.
3. Click **Generate pairing code**. A short one-time code is displayed on screen.
4. Open Telegram and start a conversation with the JARVIS bot (your administrator will have shared the bot username with you).
5. In the chat, send the command `/pair <code>` — replace `<code>` with the code from step 3 (for example, `/pair AB12CD`).
6. Return to Settings. The panel will update to show your account as **Paired**, along with your Telegram username.

Pairing is also offered as an optional step during the onboarding wizard (step 8 — Pair Telegram). If you skipped it there, you can complete it here at any time.

### Unpairing

To remove the connection, click **Unpair** in the Integrations → Telegram panel. Your library data is unaffected; only the Telegram link is removed.

---

## Telegram onboarding wizard step

If you did not complete the Telegram pairing step during the initial [Getting Started](getting-started.md) onboarding wizard, you can complete it here. The Settings panel and the wizard step are equivalent — pairing via either one links your account to the bot.

---

## Admin: configuring the bot token — Settings → Integrations → Bot Token

This section is only available to users with the **Admin** role.

The bot token is the server-level credential that authorises JARVIS to send and receive messages through the Telegram Bot API. To change the token:

1. Navigate to **Settings → Integrations → Bot Token** (admin only).
2. Enter the new bot token from the Telegram BotFather.
3. Save. The value is encrypted in the deployment database and is never shown
   again.
4. Restart the `telegram_bot` service. The bot reads its token when the
   container starts, so a replacement is not active before that restart.

All users on the instance share a single bot configured with this token. Each user's account is linked to the bot individually via the pairing flow above.

If the `telegram` Compose profile is disabled, the bot service does not start
and pairing codes cannot be consumed. Re-run setup with Telegram selected, or
enable the persisted profile on the host, before asking users to pair.

---

## Commands

The bot publishes the same command catalog to Telegram's autocomplete menu and
to `/help`. Commands that read or change research data require a paired account.
Arguments in square brackets are optional; arguments in angle brackets are
required.

| Command | What it does |
|---------|--------------|
| `/papers [query]` | List recent library papers, or search your library when a query is supplied |
| `/discover <query>` | Search arXiv, Semantic Scholar, OpenAlex and PubMed, and save the results to your library |
| `/briefing` | Show the current briefing |
| `/next` | Show the first recommendation from the current Pulse deck |
| `/inbox` | Show unread saved papers for triage |
| `/pulse_now` | Queue Pulse generation now |
| `/review` | Start a flashcard review |
| `/stats` | Show learning statistics |
| `/cancel` | Cancel the active flashcard review; this conversation-only command is intentionally absent from Telegram's global menu |
| `/projects` | List your projects, except archived ones |
| `/newproject <name>` | Create a project |
| `/tasks` | List in-progress tasks |
| `/done <id>` | Mark a task complete |
| `/focus [start [minutes] / pause / resume / stop]` | Show the focus timer, or start, pause, resume, or stop it; a started session defaults to your saved focus length and accepts 1–480 minutes |
| `/pair <code>` | Pair this Telegram chat to a JARVIS account |
| `/unpair` | Unlink this chat from its account |
| `/whoami` | Show the paired account |
| `/help` | Show this command help in Telegram |
| `/start` | Show the welcome message, or pairing guidance for an unpaired chat |

`/pulse_now` acknowledges that generation was queued; it does not claim the deck
is already finished. The job appears in the Web jobs indicator for the same
account, even when Telegram started it or it completes between browser polls.
Use `/next` or open Pulse after the job completes.

### Inline actions

The buttons below appear only where the corresponding object and state make the
action valid:

- Paper lists and detail views can offer Read more, Save, Star or Unstar,
  Reading, Done, Skip, Trash, Restore, and Trash and reject.
- Pulse and discovered-paper cards can record positive or negative feedback.
  Scheduled Pulse delivery uses Up, Down, and Save on each card.
- Project rows can open project details, and task rows can mark a task done.
- Review reminders and `/review` can start a review, reveal the answer, and rate
  recall as Again, Hard, Good, or Easy.

All actions are scoped to the paired JARVIS user. A button reports a failure
instead of silently claiming that the backend change succeeded.

---

## Pulse delivery

Telegram shows the same ranked Pulse deck as the Web interface, limited to the
first five cards for mobile readability. It labels whether the deck is current
or from an earlier date, reports its age when earlier, and states when ranking
used reduced signals. Each card reports its available evidence state: verified,
a High/Medium/Low confidence label, unverified, or not reported. These are
evidence-availability labels, not independent fact checking.

The five-card limit does not change ranking or create a separate Telegram deck.
The same paper can appear on another day when source results, relevance, and
feedback are unchanged; daily novelty is not guaranteed.

---

## Shared focus sessions

`/focus` reports the same per-user focus interval shown by the Web TopBar
timer: its state and remaining time, today's focused minutes against your
daily target, and your streak. `/focus start` begins an interval at your saved
focus length unless you pass an explicit number of minutes, and `/focus pause`,
`/focus resume`, and `/focus stop` drive the running one. Either client can
drive the session. Starting another interval while one is active is refused,
and completion time is recorded once even if both clients observe it.

While the interval is active or paused, JARVIS suppresses scheduled morning
briefings, paper digests, review reminders, deadline warnings, Pulse delivery,
and author alerts for that user. The focus-completion notice and operator-error
messages remain available. If the bot cannot confirm focus state, scheduled
delivery stays suppressed rather than breaking the pause promise.

Timer durations and the daily cycle target are account preferences, so
Telegram uses the same values as the Web timer; browser notification permission
stays device-local. The active focus interval and its accounting are shared
server state.

---

## Related pages

- [Settings](settings.md) — full settings reference including the Integrations section.
- [Getting Started](getting-started.md) — onboarding wizard step 8 covers Telegram pairing.
- [Pulse](pulse.md) — Pulse digests are delivered via Telegram to paired accounts.
