<!-- verified-against-UI: 2026-07-13 | routes: setup.sh (terminal chooser), /auth/verify, /settings?section=account -->

# Choosing how you access JARVIS

The very first question `./setup.sh` asks is **"How will you access JARVIS?"** Your answer decides where you can open the dashboard, which sign-in options each device gets, and whether your browser shows a one-time certificate notice. This page explains the four choices in plain language so you can pick confidently — and change your mind later without redoing anything else.

There is nothing to edit by hand: setup derives all the address and security settings from your one answer and writes them itself.

---

## The four modes at a glance

| Mode | Where you can open JARVIS | Sign-in links (email) | Passkeys (fingerprint / face / PIN) | Certificate notice? |
|------|--------------------------|----------------------|--------------------------------------|---------------------|
| **1. On this computer only** | The machine JARVIS runs on | ✅ | ✅ on this computer | Once, first visit |
| **2. Home or lab network** | Any device on your network | ✅ on every device | ✅ on the JARVIS computer only | Once per device |
| **3. Cloudflare Tunnel** | Anywhere on the internet | ✅ | ✅ everywhere | No |
| **4. Your own domain (Let's Encrypt)** | Anywhere on the internet | ✅ | ✅ everywhere | No |

Whichever you pick, staying signed in works the same way everywhere: a sign-in lasts 30 days and quietly renews while you keep using JARVIS, so you won't be logged out mid-project.

---

## What each mode means

### 1) On this computer only (recommended to start)

JARVIS is reachable only from the machine it is installed on. Nothing is opened to your network or the internet. Everything works here: sign-in links, passkeys, the lot. This is the safest default, and you lose nothing by starting here — you can re-run setup and move to any other mode whenever you're ready.

### 2) From devices on your home or lab network

The dashboard becomes reachable from other devices on the same network — your laptop on the sofa, a tablet in the lab — at an address like `https://192.168.1.20:3001` (setup detects and prints your address for you, and checks it is reachable at the end).

Two things to know:

- **Sign-in links work on every device**; passkeys keep working on the JARVIS computer itself. Other devices sign in with email links — that's a browser rule about numeric addresses, not a JARVIS limitation (see [Passkeys vs sign-in links](#passkeys-vs-sign-in-links) below).
- Each device shows a **one-time certificate notice** on first visit — expected for a private setup, explained below.

Setup will remind you of this too: LAN mode makes the dashboard reachable by every device on your network, so use it only on a network you trust. On shared or untrusted networks, prefer option 3 or a VPN such as Tailscale (see the access-mode comparison in [DEPLOYMENT.md](../DEPLOYMENT.md)).

### 3) From anywhere — Cloudflare Tunnel

Gives JARVIS a real web address (like `jarvis.yourname.com`) reachable from anywhere, without opening any ports on your router. Full features everywhere, including passkeys, and no certificate notices — visitors get a proper trusted certificate at your tunnel address.

You'll need a **free Cloudflare account** and a tunnel token; setup guides you through both. Because a tunnel exposes your JARVIS to the internet, setup first asks you to configure **Zero Trust access policies** in your Cloudflare dashboard and to type `I understand` before it continues — a deliberate speed bump, since this is the step that takes your instance public. It then asks for the public hostname you configured so everything lines up automatically.

### 4) From anywhere — your own domain with Let's Encrypt

Like the tunnel, but using a domain you own that points directly at your machine. Setup asks for your **domain** and an **admin email** (Let's Encrypt sends certificate-expiry notices there), and JARVIS obtains a real, trusted certificate automatically. Full features everywhere, including passkeys, no certificate notices.

This mode needs your domain's DNS pointing at the machine and **port 443 reachable** from the internet — typically a port-forward on your router or a machine with a public address. If that sounds like homework, option 3 gets you the same result with no router changes.

---

## About the one-time certificate notice

In modes 1 and 2, JARVIS creates its own HTTPS certificate on first start. Your connection is fully encrypted — but because the certificate was made by your JARVIS rather than issued by a public authority, your browser shows a warning the first time each device visits. Accept it once per device and it won't reappear:

- **Chrome / Edge** — type `thisisunsafe` on the warning page (there's no input box; just type it).
- **Firefox** — click **Advanced → Accept the Risk and Continue**.
- **Safari** — click **Show Details → visit this website** (macOS asks for your password).

Modes 3 and 4 use real, publicly-trusted certificates at your public address, so there is no notice.

If you ever see the notice again after changing access modes, that's normal — the certificate was regenerated to cover your new address (see below).

---

## Passkeys vs sign-in links

JARVIS offers two everyday ways to sign in (plus API-key login for single-user installs — unaffected by any of this):

- **Sign-in links** (magic links by email) work in **every mode, on every device**. They are always your fallback.
- **Passkeys** — fingerprint, face, or device PIN — are the fastest option, but browsers only allow them on a proper named address: `localhost` on the JARVIS machine itself, or a real domain like modes 3 and 4 provide. A numeric network address (`https://192.168.x.x`) doesn't qualify, which is why LAN-mode devices other than the JARVIS computer stick to sign-in links.

You don't need to remember any of this: the sign-in page checks what your current address supports and only shows the passkey button where it will actually work — elsewhere it shows a short note instead. You can add and manage your passkeys under **Settings → Account → Passkeys**.

---

## Switching modes later

Re-run setup and give a different answer to the first question:

```bash
./setup.sh
```

That's the whole procedure. Your papers, notes, settings, and accounts are untouched — only the access configuration changes. Setup handles the follow-through for you:

- It detects that your address set changed and offers to **regenerate the HTTPS certificate** so it covers the new address (the dashboard restarts briefly). Say yes — declining leaves the old certificate in place and browsers may complain until you regenerate.
- In modes 1 and 2, expect the **one-time certificate notice** again on each device after the regeneration.
- Moving up to mode 3 or 4 walks you through the extra pieces (Cloudflare consent + token, or domain + admin email) right in the flow.

A common path: start with mode 1 on day one, switch to mode 2 when you want JARVIS on your tablet, and later move to mode 3 or 4 for access away from home — picking up passkeys on every device along the way.

---

## What comes next

New here? [Getting Started](getting-started.md) covers the onboarding wizard and your first sign-in. For operator-level detail — ports, VPN alternatives like Tailscale, and TLS specifics — see [DEPLOYMENT.md](../DEPLOYMENT.md).