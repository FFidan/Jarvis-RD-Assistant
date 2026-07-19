<!-- verified-against-UI: 2026-07-19 | routes: setup.sh (terminal chooser), /auth/verify, /settings?section=account -->

# Choosing how you access JARVIS

The very first question `./setup.sh` asks is **"How will you access JARVIS?"** Your answer decides where you can open the dashboard and which sign-in options each device gets. This page explains the choices in plain language, and gives you the two real ways to reach JARVIS from another device — a bootstrap route and a durable family route — so you can pick confidently.

There is nothing to edit by hand: setup derives the address and network settings from your answer and writes them itself.

---

## The four modes at a glance

| Mode | Where you can open JARVIS | Sign-in links (email) | Passkeys (fingerprint / face / PIN) | Transport |
|------|--------------------------|----------------------|--------------------------------------|-----------|
| **1. On this computer only** | The machine JARVIS runs on | ✅ | ✅ on this computer | Plain HTTP over loopback (`http://localhost:3001`) |
| **2. Home or lab network** | Any device on your network | ✅ on every device | ✅ on the JARVIS computer only | Plain HTTP over your LAN (`http://<lan-ip>:3001`) — see [About LAN mode](#about-lan-mode) below |
| **3. Cloudflare Tunnel** | Anywhere on the internet | ✅ | ✅ everywhere | HTTPS, edge TLS terminated by Cloudflare |
| **4. Your own domain (Let's Encrypt)** | Anywhere on the internet | ✅ | ✅ everywhere | HTTPS, a real Let's Encrypt certificate |

Whichever you pick, staying signed in works the same way everywhere a session can persist: a sign-in lasts 30 days and quietly renews while you keep using JARVIS, so you won't be logged out mid-project. Raw-IP LAN access (mode 2, browsed directly by IP) is the one exception — see below.

---

## What each mode means

### 1) On this computer only (recommended to start)

JARVIS is reachable only from the machine it is installed on, at `http://localhost:3001`. Nothing is opened to your network or the internet. Everything works here: sign-in links, passkeys, the lot. This is the safest default, and you lose nothing by starting here — you can re-run setup and move to any other mode whenever you're ready.

Want HTTPS on this machine too (a locally-trusted certificate, no browser warning)? Run `./setup.sh --profile=local-https` — it starts a local Caddy edge at `https://localhost:3443` using a certificate from [mkcert](https://github.com/FiloSottile/mkcert) (installed to your system trust store by `make certs`).

### 2) From devices on your home or lab network

The dashboard becomes reachable from other devices on the same network — your laptop on the sofa, a tablet in the lab — at `http://<your-lan-ip>:3001` (setup detects and prints your address, and checks it is reachable at the end).

Setup will remind you of this too: LAN mode binds the dashboard to every interface over **plain HTTP**, reachable by every host on your network — use it only on a network you trust. See [About LAN mode](#about-lan-mode) for what this does and does not give you, and for the two real ways to get a durable, authenticated family route on top of it.

### 3) From anywhere — Cloudflare Tunnel

Gives JARVIS a real web address (like `jarvis.yourname.com`) reachable from anywhere, without opening any ports on your router. Full features everywhere, including passkeys — Cloudflare terminates a proper trusted certificate at your tunnel address.

You'll need a **free Cloudflare account** and a tunnel token; setup guides you through both. Because a tunnel exposes your JARVIS to the internet, setup first asks you to configure **Zero Trust access policies** in your Cloudflare dashboard and to type `I understand` before it continues — a deliberate speed bump, since this is the step that takes your instance public. It then asks for the public hostname you configured so everything lines up automatically.

### 4) From anywhere — your own domain with Let's Encrypt

Like the tunnel, but using a domain you own that points directly at your machine. Setup asks for your **domain** and an **admin email** (Let's Encrypt sends certificate-expiry notices there), waits for the certificate to be issued, and only then reports success — JARVIS never claims an HTTPS route that isn't actually serving yet. Full features everywhere, including passkeys.

This mode needs your domain's DNS pointing at the machine and **port 443 reachable** from the internet — typically a port-forward on your router or a machine with a public address. If that sounds like homework, option 3 gets you the same result with no router changes.

---

## About LAN mode

Mode 2 binds the dashboard to `0.0.0.0` and serves plain HTTP on your network. That gets you two things and explicitly **not** a third:

- **Viewing works.** Any device on the LAN can open `http://<lan-ip>:3001` and use the app once a session already exists on that device.
- **The one-time setup link is not for this.** The click-to-finish link setup prints always stays on `http://localhost` — the setup token rides that link, and a bearer token must never cross a shared network in plaintext. Finish first-admin setup from the server itself, or forward the port over SSH:

  ```bash
  ssh -L 3001:127.0.0.1:3001 user@host
  ```

  then open the forwarded `http://localhost:3001` link.

- **Signed-in sessions do not persist over the raw IP.** Session cookies are `Secure`, so they are dropped by the browser on plain `http://<ip>` — a sign-in there will not stick, and passkeys cannot be registered against a numeric IP at all (WebAuthn requires a real hostname; see [Passkeys vs sign-in links](#passkeys-vs-sign-in-links)). Raw-IP LAN is a fine way to *view* JARVIS from a second device with a link someone hands you, but it is not an authenticated family route.

For a durable, authenticated route to other devices, add a named private HTTPS origin on top of LAN (or localhost) mode:

```bash
./setup.sh --public-origin https://<host>.<tailnet>.ts.net
```

The reference walkthrough is [Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve) — install Tailscale on the host, run `tailscale serve --bg --https=443 http://127.0.0.1:3001`, and pass that HTTPS URL to `--public-origin`. Setup adds the hostname to `APP_BASE_URL`, `CORS_ORIGINS`, and the dashboard's Host allowlist, then probes the origin; once it answers, the click-to-finish setup link is hosted there instead of loopback, and sign-ins and passkeys work exactly like modes 3 and 4. Any other named-HTTPS proxy that terminates TLS and forwards to the dashboard works the same way — Tailscale Serve is simply the easiest to set up.

---

## Passkeys vs sign-in links

JARVIS offers two everyday ways to sign in (plus API-key login for single-user installs — unaffected by any of this):

- **Sign-in links** (magic links by email) work wherever a session can persist — every mode except raw-IP LAN browsing (see above). They are always your fallback.
- **Passkeys** — fingerprint, face, or device PIN — are the fastest option, but browsers only allow them on a proper origin: `localhost` on the JARVIS machine itself, or the exact HTTPS origin configured in `APP_BASE_URL` (modes 3, 4, or a named private origin). A numeric network address (`http://192.168.x.x`) is never a valid passkey origin, and enrolling a passkey only works at the **final** origin you'll actually sign in from — not a temporary one.

You don't need to remember any of this: the sign-in page checks what your current address supports and only shows the passkey button where it will actually work — elsewhere it shows a short note instead. You can add and manage your passkeys under **Settings → Account → Passkeys**.

---

## Support tiers

| Tier | Routes | What it means |
|---|---|---|
| **Supported** | localhost HTTP, named private HTTPS (Tailscale Serve or equivalent), Let's Encrypt, Cloudflare Tunnel | Fully tested, gets the standard hardening and cookie/passkey behavior. |
| **Experimental** | Local HTTPS (`--profile=local-https`, mkcert at `:3443`); AMD/Intel Vulkan or ROCm GPU acceleration | Works, lower validation confidence — report issues. |
| **Manual** | Custom reverse proxy (Traefik, ngrok, host nginx, …) | Works if you satisfy the [trust-boundary contract](../DEPLOYMENT.md#per-adapter-trust-contract) yourself — JARVIS does not configure or verify a proxy it didn't set up. |
| **Unsupported for sign-in** | Raw-IP LAN browsing | Viewing only — see [About LAN mode](#about-lan-mode). No persistent session, no passkeys. |

The table below is generated from the same route registry the app enforces (`route_claims` in `scripts/setup_lib.sh`); a docs test keeps the two in lockstep so this page can never claim a tier or behavior the code doesn't grant.

<!-- route-claims:begin -->
| route | scheme | port | host_allowlist | setup_token_transport | cookie_policy | passkey_origin | cert_owner | tier |
|---|---|---|---|---|---|---|---|---|
| localhost-http | http | 3001 | localhost | fragment | secure | localhost | none | supported |
| raw-ip-lan | http | 3001 | lan-ip | paste | none | none | none | supported |
| named-private-https | https | 443 | origin-host | fragment | secure | origin-host | external | supported |
| local-https | https | 3443 | localhost | fragment | secure | localhost | mkcert | experimental |
| letsencrypt | https | 443 | domain | fragment | secure | domain | letsencrypt | supported |
| tunnel | https | 443 | tunnel-host | fragment | secure | tunnel-host | cloudflare | supported |
<!-- route-claims:end -->

`tier` above is the route's own support classification (an unauthenticated `http://` view of the raw LAN route is "supported" because it works as designed) — it is a different axis from **cookie_policy**/**passkey_origin**, which is why raw-IP LAN shows `none`/`none`: the route serves pages, it just cannot carry a signed-in session.

---

## Switching modes later

Re-run setup and give a different answer to the first question:

```bash
./setup.sh
```

That's the whole procedure. Your papers, notes, settings, and accounts are untouched — only the access configuration changes. Setup handles the follow-through for you:

- It re-derives `CORS_ORIGINS` and the dashboard's Host allowlist for the new mode, accumulating rather than dropping — a named private origin you configured earlier stays allowed alongside a new LAN or tunnel choice.
- Moving up to mode 3 or 4 walks you through the extra pieces (Cloudflare consent + token, or domain + admin email) right in the flow.
- If you're adding `--profile=local-https`, generate the mkcert certificate first with `make certs` — setup tells you if it's missing.

A common path: start with mode 1 on day one, switch to mode 2 when you want JARVIS on your tablet, and add a named private HTTPS origin (or move to mode 3 or 4) for a durable route away from home — picking up passkeys on every device along the way.

---

## What comes next

New here? [Getting Started](getting-started.md) covers the onboarding wizard and your first sign-in. For operator-level detail — ports, the full trust-contract table per adapter, and TLS specifics — see [DEPLOYMENT.md](../DEPLOYMENT.md).
