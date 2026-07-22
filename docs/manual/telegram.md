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

## Related pages

- [Settings](settings.md) — full settings reference including the Integrations section.
- [Getting Started](getting-started.md) — onboarding wizard step 8 covers Telegram pairing.
- [Pulse](pulse.md) — Pulse digests are delivered via Telegram to paired accounts.
