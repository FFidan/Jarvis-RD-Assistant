<!-- verified-against-UI: 2026-07-13 | routes: /login, /settings?section=account&item=passkeys -->

# Passkeys

A **passkey** lets you sign in to JARVIS with your fingerprint, face, or device PIN instead of waiting for an email link. Once you have added a passkey on a device, signing in from that device takes one tap.

---

## What is a passkey, and why use one?

A passkey is a sign-in credential that lives on your device — your laptop, phone, or a hardware security key. When you sign in, your device asks you to confirm with your fingerprint, face, or PIN, and that's it: no email to open, no link to click, nothing to type or remember.

Passkeys are worth setting up because they are:

- **Faster** — one confirmation on your device replaces the request-email-open-link round trip of a magic link.
- **Safer** — a passkey only works on your JARVIS address. There is no password or link that could be intercepted or entered on a look-alike site.
- **Per-device** — each passkey belongs to one device. Add one on every device you regularly use; removing one never affects the others.

Passkeys are optional. Magic-link (and, where enabled, API-key) sign-in always remains available, whether or not you use passkeys.

---

## Where passkeys are available

Passkeys need a web address the browser can trust, so they are offered only where they can actually work:

- **On the JARVIS computer itself** (a `localhost` install) — passkeys work.
- **Through a configured domain** — a secure (HTTPS) address set up by your operator, such as the "From anywhere" tunnel option or your own domain — passkeys work from any device.
- **On a LAN install reached by a raw IP address** — passkeys are **not** available. The sign-in page keeps magic links and shows a short note instead: *"Passkeys work on the JARVIS computer itself, or from everywhere once you enable the 'From anywhere' access option."*

Your browser must also support passkeys (all current mainstream browsers do). Where passkeys cannot work, JARVIS never shows a broken button — the passkey controls simply don't appear, or a one-line note explains why.

---

## Adding a passkey to your account

<!-- screenshot: Settings → Account → Passkeys, showing the "Add a passkey" button and a list entry "Chrome on macOS — Added 2 days ago · Last used 3 hours ago" -->

1. Sign in as usual (magic link or API key).
2. Open **Settings → Account → Passkeys**.
3. Click **Add a passkey**.
4. Optionally edit the **Name** — it is pre-filled with something recognisable like "Chrome on macOS" so you can tell your devices apart later.
5. Click **Create passkey**. Your device asks for your fingerprint, face, or PIN to create it.
6. A "Passkey added." confirmation appears, and the new passkey shows up in the list.

Repeat on each device you use — one passkey per device.

> **Tip:** after you request a magic link on a device where passkeys work, the sign-in page shows a one-time suggestion — *"Make sign-in easier on this device"* — reminding you to add a passkey from Settings. You can dismiss it, and it won't come back.

If you see *"This device already has a passkey for your account"*, there is nothing to do — this device is already set up.

---

## Signing in with a passkey

<!-- screenshot: LoginPage showing the "Sign in with a passkey" button below the magic-link form, separated by an "or" divider -->

1. Open the sign-in page. Below the usual sign-in form, after an "or" divider, you'll find **Sign in with a passkey**. (The button only appears where passkeys work — see [Where passkeys are available](#where-passkeys-are-available).)
2. Click it and confirm with your fingerprint, face, or device PIN. JARVIS always asks for this confirmation — simply having the device unlocked is not enough.
3. You're in.

If you cancel or the prompt times out, you'll see *"Sign-in was cancelled or timed out. You can try again."* with a **Try again** link — nothing is lost. If a prompt sat open too long and expired, just start again from the button. And if a passkey won't work on this device at all, use the magic-link form on the same page instead.

---

## Managing your passkeys

**Settings → Account → Passkeys** lists every passkey on your account with its name, when it was added, and when it was last used.

To remove one, click the trash icon next to it and confirm:

- Removing a passkey means you can no longer sign in with that device's passkey. **Your other passkeys keep working.**
- If it is your **only** passkey, the confirmation says so explicitly: after removing it you'll sign in with a magic link or API key until you add a new one.

Remove a passkey whenever you retire a device, hand it to someone else, or simply no longer recognise an entry in the list.

---

## If you lose a device

You are never locked out: **magic-link sign-in always works**, even if the lost device held your only passkey.

1. On any other device, sign in with a magic link as usual.
2. Go to **Settings → Account → Passkeys** and remove the lost device's passkey so it can no longer be used.
3. Add a fresh passkey on your replacement device when you're ready.

If you'd rather have everything cleared at once — for example after a stolen device — an administrator can remove **all** passkeys from your account from the admin user-management page; you then sign in with a magic link and re-register.

> **Automatic protection:** if JARVIS ever detects that one of your passkeys has been copied onto another device, it removes that passkey automatically and ends any sessions it started. If that happens, sign in with a magic link and add a new passkey.

---

## Related pages

- [Getting Started](getting-started.md) — the sign-in flow and magic links.
- [Settings](settings.md) — the Account section, where the Passkeys page lives.
- [Admin & Multi-tenant](admin.md) — user management, where admins can revoke a user's passkeys.
