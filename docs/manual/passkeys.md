<!-- verified-against-UI: 2026-07-20 | routes: /login, /settings?section=account&item=passkeys, /admin/users -->

# Passkeys

A passkey lets you sign in with the fingerprint, face, device PIN, password
manager, or security key offered by your browser. It is optional and has no
JARVIS password to remember.

---

## Where passkeys are available

Passkeys need a web address the browser can trust, so they are offered only where they can actually work:

- **On the JARVIS computer itself:** use `http://localhost:3001`.
- **Through a configured domain:** use the exact named HTTPS address selected
  during setup.
- **At a numeric LAN address:** the app is not available. An address such as
  `http://192.168.1.20:3001` serves only `/health/jarvis`; all other paths return
  HTTP 403.

Where a passkey cannot work, JARVIS hides the control and explains which address
to use. Configure a named private HTTPS route, Cloudflare Tunnel, or your own
HTTPS domain for family devices.

---

## Adding a passkey to your account

<!-- screenshot: Settings → Account → Passkeys, showing the "Add a passkey" button and a list entry "Chrome on macOS — Added 2 days ago · Last used 3 hours ago" -->

1. Sign in through the finish-setup link, an invitation, an administrator-shared
   link, delivered email, or the owner's API-key recovery path.
2. Open **Settings → Account → Passkeys**.
3. Click **Add a passkey**.
4. Optionally edit the **Name** — it is pre-filled with something recognisable like "Chrome on macOS" so you can tell your devices apart later.
5. Click **Create passkey**. Your device asks for your fingerprint, face, or PIN to create it.
6. A "Passkey added." confirmation appears, and the new passkey shows up in the list.

Add another passkey from each browser or authenticator you need. Some operating
systems and password managers sync passkeys between your own devices; that is
controlled by the provider, not by JARVIS.

> **Tip:** after magic-link sign-in on a supported device, JARVIS offers a
> one-time reminder to add a passkey. Dismissing it hides it on that device.

If you see *"This device already has a passkey for your account"*, there is nothing to do — this device is already set up.

---

## Signing in with a passkey

<!-- screenshot: LoginPage showing the "Sign in with a passkey" button below the magic-link form, separated by an "or" divider -->

1. Open the sign-in page. Below the usual sign-in form, after an "or" divider, you'll find **Sign in with a passkey**. (The button only appears where passkeys work — see [Where passkeys are available](#where-passkeys-are-available).)
2. Click it and confirm with your fingerprint, face, or device PIN. An unlocked
   device alone does not complete the confirmation.
3. JARVIS opens your account.

If you cancel or the prompt expires, use **Try again**. If the passkey does not
work on that device, request an emailed sign-in link when email is configured.
Otherwise ask an administrator for a private 15-minute sign-in link.

---

## Managing your passkeys

**Settings → Account → Passkeys** lists every passkey on your account with its name, when it was added, and when it was last used.

To remove one, click the trash icon next to it and confirm:

- Removing a passkey means you can no longer sign in with that device's passkey. **Your other passkeys keep working.**
- If it is your **only** passkey, the confirmation says so explicitly. Before
  removing it, confirm that one of the recovery paths below works.

Remove a passkey when you retire or transfer a device, or when you no longer
recognise an entry in the list.

---

## After a restore or hostname change

A data restore preserves accounts and registered passkeys, but WebAuthn still
binds each credential to its original **RP ID**, which is the hostname used when
the passkey was created. Keep the same hostname and configure the matching
`APP_BASE_URL` on a replacement host if existing passkeys must continue to
work.

If the hostname changes, an old passkey cannot authenticate to the new RP ID.
Recover with email, a private administrator link, or the owner-only operations
key, then re-register a passkey at the new named HTTPS address. Changing only
the server data does not retarget an existing credential.

---

## Recovery

Recovery depends on how the instance is operated:

- **Email is working:** request a magic link from the sign-in page.
- **Email is not configured:** a signed-in administrator opens **Admin → User
  Management**, uses **Send sign-in link** on the user's row, and shares the
  returned 15-minute link privately.
- **Instance owner:** the operations API key can recover the configured owner on
  localhost or the final HTTPS origin. Keep it available only to the host
  operator. It is not a shared login for other users; do not send it with an
  invitation.

If the only administrator has no working session, no passkey, and no email
delivery, other users cannot generate that administrator's recovery link.
Avoid that state: keep a second administrator or a second owner passkey, and
keep the API key available to the operator.

After a device is lost, sign in through a recovery path and remove its passkey.
An administrator can also revoke all passkeys on a user's account. The user must
then recover the account and register a new passkey.

---

## Related pages

- [First sign-in and setup](getting-started.md) — the sign-in and invitation flow.
- [Settings](settings.md) — the Account section, where the Passkeys page lives.
- [Admin and multi-user operation](admin.md) — manual sign-in links and passkey revocation.
