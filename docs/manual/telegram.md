<!-- verified-against-UI: 2026-05-18 | routes: /settings?section=integrations&item=telegram, /settings?section=integrations&item=bot-token -->

# Telegram

JARVIS RD Assistant integrates with Telegram so you can receive Pulse digests and stay connected to your library from a mobile device. Each user pairs their personal Telegram account individually; the Telegram bot itself is configured by an admin.

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

Pairing is also offered as an optional step during the post-login setup wizard (step 6). If you skipped it there, you can complete it here at any time.

### Unpairing

To remove the connection, click **Unpair** in the Integrations → Telegram panel. Your library data is unaffected; only the Telegram link is removed.

---

## Telegram setup wizard step

If you did not complete the Telegram pairing step during the initial [Getting Started](getting-started.md) setup wizard, you can return to it here. The Settings panel and the setup wizard step are equivalent — completing either one marks the step as done.

---

## Admin: configuring the bot token — Settings → Integrations → Bot Token

This section is only available to users with the **Admin** role.

The bot token is the server-level credential that authorises JARVIS to send and receive messages through the Telegram Bot API. To change the token:

1. Navigate to **Settings → Integrations → Bot Token** (admin only).
2. Enter the new bot token from the Telegram BotFather.
3. Save. The change takes effect immediately; a service restart is not required.

All users on the instance share a single bot configured with this token. Each user's account is linked to the bot individually via the pairing flow above.

---

## Related pages

- [Settings](settings.md) — full settings reference including the Integrations section.
- [Getting Started](getting-started.md) — setup wizard step 6 covers Telegram pairing.
- [Pulse](pulse.md) — Pulse digests are delivered via Telegram to paired accounts.
