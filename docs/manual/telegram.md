<!-- verified-against-UI: 2026-08-19 | routes: /settings?section=integrations&item=telegram -->

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

## One settings page

Everything Telegram lives on a single page: **Settings → Integrations →
Telegram**. It opens with **Your Telegram**, the personal pairing controls
every account has. Administrators see a second block below it, **Instance bot
(admin)**, holding the BotFather token for the whole deployment. Bookmarks to
the old separate Bot Token item land on this same page.

The division is worth keeping in mind: the block at the top is *your* chat, the
block at the bottom is *the instance's* bot, and the two are configured
independently.

---

## Pairing your account

1. Open **Settings → Integrations → Telegram**.
2. **Your Telegram** shows your current pairing state. If you are not paired, it offers **Generate pairing code**.
3. Click it. A one-time code appears, valid for 15 minutes; the panel then waits for the bot to confirm. **Regenerate token** gets you a fresh one if it expires first.
4. Open Telegram and start a conversation with your instance's bot — your administrator will have shared its username.
5. Send `/pair <code>`, replacing `<code>` with the code on screen, for example `/pair 8f2c1d9ab4e07356…`. It has to be a private one-to-one chat: in a group every member shares the chat identity, so pairing there would hand your account to all of them, and the bot refuses.
6. The settings panel updates to **Paired** with your Telegram username. `/whoami` in the chat confirms the same thing.

A chat can belong to only one account, and an account to only one chat. Pairing an account to a new chat displaces the old one, and the previous chat is notified that this happened.

Pairing is also offered as an optional step in the onboarding wizard. The wizard step and this panel do the same thing; if you skipped it there, do it here.

### Unpairing

Click **Unpair** in the same panel, or send `/unpair` in the chat. Your library data is unaffected — only the link between the chat and your account is removed.

---

## Admin: the instance bot token — Settings → Integrations → Telegram

The **Instance bot (admin)** block on that page is visible only to accounts with the **Admin** role.

The bot token is the server-level credential that authorises JARVIS to send and receive messages through the Telegram Bot API. To change it:

1. Open **Settings → Integrations → Telegram** and scroll to **Instance bot (admin)**.
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
| `/next` | Show the next Pulse card you have not acted on |
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

`/next` walks the deck. Cards arrive ranked, and each one carries its own state
for your account, so `/next` skips whatever you have already saved, read, or
rated — including from the web deck — and hands you the highest-ranked card
still untouched. Nothing is remembered between calls, so the two clients cannot
drift apart. When you have acted on every card it says so and links to the full
deck.

`/pulse_now` acknowledges that generation was queued; it does not claim the deck
is already finished. The job appears in the Web jobs indicator for the same
account, even when Telegram started it or it completes between browser polls.
Use `/next` or open Pulse after the job completes.

Where a command prints a project's status, it uses the same words the web app
does: **In progress**, **Draft**, **Completed**, and **Archived**. `/projects`
lists everything except Archived, because archiving is the one status that means
"put away deliberately".

`/briefing` states the window and the view behind each number rather than
printing bare counts: papers added to your library since midnight UTC, papers
currently waiting in the Inbox view, and cards due right now. Its open-task
count uses the same not-done rule as My Day — to do, in progress, or blocked —
so the two surfaces cannot disagree about how much is outstanding.

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
- [Getting Started](getting-started.md) — the onboarding wizard offers Telegram pairing as its last optional step.
- [Pulse](pulse.md) — Pulse digests are delivered via Telegram to paired accounts.
