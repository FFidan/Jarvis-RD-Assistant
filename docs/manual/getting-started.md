<!-- verified-against-UI: 2026-08-19 | routes: /onboarding, /auth/verify, /admin/users -->

# First sign-in and setup

This page starts after `./setup.sh` has brought the services up. If JARVIS is
not installed yet, follow the [install steps](../readme-shim.md).

## Open the finish-setup link

At the end of installation, `setup.sh` prints a line labelled **Finish setup**.
Open that exact link. It carries the one-time token that protects creation of
the first administrator. The token is removed from the address bar after the
page opens.

Use one of these safe routes:

- On the JARVIS computer, open the exact localhost finish link printed by setup.
  It is usually `http://localhost:3001`; the local-HTTPS profile uses
  `https://localhost:3443`.
- From another device, use the verified named HTTPS address that setup prints.
- If setup is running through SSH, or the server has no desktop, setup prints an
  SSH-forward command. When local HTTPS belongs to a VM or server, setup also
  prints an HTTP localhost finish link for the outside browser. Run the command
  on your computer, keep it open, then open that exact link in your browser.
  HTTP stays inside the encrypted SSH connection. Do not change that address to
  `https`; the outside browser does not need the VM's certificate.

Do not paste the setup token into a plain LAN address such as
`http://192.168.1.20:3001`. From another device, that address serves only
`/health/jarvis`; every app, setup, and sign-in path returns HTTP 403. See
[Access from other devices](access-modes.md).

## Create the first administrator

The onboarding order depends on the sign-in mode selected during installation:

- **Single-user:** system check, create the administrator and sign in, then
  optional email setup.
- **Multi-user:** system check, optional email setup, then create the
  administrator and sign in.

The first administrator does not need an email round trip. Enter the account
email and choose **Create admin & sign in**. The protected setup request creates
the account, records it as the instance owner, and starts its browser session.

Email is optional in both modes. In multi-user mode it automates delivery of
sign-in links; without it, the administrator can create a one-time link in
**Admin → User Management** and share it privately. Email can be configured
later under **Settings → System → Email**.

## Finish the wizard

After sign-in, the wizard offers the remaining instance settings in this order:

1. Optional cloud-model provider keys.
2. A first research topic.
3. The Pulse schedule.
4. Optional research-source API keys.
5. Optional Telegram pairing.

Local Ollama models, arXiv, and the core research workflow do not require a
cloud-model key. Source keys can raise third-party rate limits but are not
required for the first paper. Every optional item can be configured later from
Settings.

## Add family or team members

Set up the final address before sending invitations. Everyone must use the same
named HTTPS origin for persistent sessions and passkeys.

1. Sign in as an administrator at the final HTTPS address.
2. Open **Admin → User Management** and choose **Invite user**.
3. Enter the person's email address and role.
4. If email is configured, JARVIS sends the 24-hour invitation. Otherwise it
   shows the same one-time link for you to copy and share through a private
   channel.
5. The invited person opens the link at that HTTPS address and signs in.
6. They add a passkey under **Settings → Account → Passkeys**.

Do not post an invitation in a group chat or shared document. Anyone holding an
unused link can use it until it expires.

## Signing in later

- **Single-user owner:** use the API key stored during setup, or a passkey after
  adding one.
- **Multi-user with email:** request a sign-in link, or use a passkey.
- **Multi-user without email:** use a passkey. If a user needs recovery, an
  administrator creates a fresh 15-minute sign-in link from that user's row and
  shares it privately.

The instance owner can use the API key recovery path on localhost or the final
HTTPS origin. Other users cannot use the operations API key as a shared login.
See [Passkeys](passkeys.md) before removing a last passkey. If an upgraded
multi-admin installation has no valid owner, the host operator must repair it
explicitly; see [Admin Pages](admin.md#instance-ownership-and-recovery).

## Add the first paper

Open **Discover** and search by title or keyword across your enabled sources,
then choose **Save to your papers** on the result you want — or use **Upload
PDF** to add a file directly. Then open the paper and choose **Analyze Paper**
to download and parse the PDF, create search embeddings, and generate the
configured research outputs.

GPU analysis usually takes a few minutes. CPU analysis can take considerably
longer, especially for a long paper. The job panel reports each stage and keeps
partial failures separate from completed work.

After analysis:

- the paper page shows its summary, and each finding links back to the page and
  passage it came from;
- **Ask** can retrieve from analyzed papers in your library;
- cards generated from the paper appear under **Learning Cards**;
- extracted entities can appear in the Knowledge Graph.

Continue with [Papers & Discover](research-feed.md), or read
[Navigation](navigation.md) for a tour of the workspace.
