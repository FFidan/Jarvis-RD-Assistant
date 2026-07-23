<!-- verified-against-UI: 2026-07-19 | routes: app-wide -->

# Install the web app and use it offline

JARVIS is a Progressive Web App: you can install it like a native app on your
phone, tablet, or computer. Previously loaded parts of the library remain
available when the device loses its connection.

The offline cache requires a secure browser context: use HTTPS, or access
JARVIS as `localhost` on the machine where it runs. A plain LAN IP exposes only
`/health/jarvis`, not the web app, and cannot install a service worker. See
[Choosing how you access JARVIS](access-modes.md) for the supported options.

---

## Installing

### Android, and desktop Chrome / Edge

When the browser is ready to install JARVIS, a slim **Install JARVIS**
banner appears near the top of the app, below the connectivity banner.

1. Click **Install**.
2. Confirm in the browser's own install prompt.

JARVIS then opens in its own window with its own icon. If you dismiss the
banner, it stays dismissed on
that device; you can still install later from the browser's own menu (for
example Chrome's "Install app" item, usually behind the three-dot menu or an
icon in the address bar).

### iOS / iPadOS (Safari)

iOS does not offer the automatic install banner, but Safari's built-in
"Add to Home Screen" does the same job:

1. Open JARVIS in **Safari**.
2. Tap the **Share** button (the square with an arrow pointing up).
3. Scroll down and tap **Add to Home Screen**.
4. Confirm the name and tap **Add**.

A JARVIS icon now appears on your Home Screen. Opening it launches JARVIS in
its own window, without Safari's address bar or tab chrome.

---

## What works offline

After you have loaded supported pages while online, their saved data remains
available if your device loses its connection. A slim banner appears at the
top of the app to let you know you're offline and viewing saved data.

**Available offline:**

- Previously loaded **Library** pages — the paper list and per-status counts.
  Inbox and Discover still require a connection.
- **Paper detail** for papers you've previously opened — metadata,
  abstract, and any summary already generated.
- **Notes** you've already saved on a paper (read-only offline; see the editing
  limitations below).
- The **structured extraction table**, and your **dashboard** metrics and
  reading stats.
- If a **Learning Card is already open** when the connection drops, its rating
  is queued on your device and sent to the server when you reconnect. Loading
  the next card still requires a connection, so this is recovery for an
  interrupted review rather than a full offline review session.

**Not available offline** — these all need a live model or a live network
call, so JARVIS never pretends to serve them from a cache:

- **PDFs.** Reading the original PDF of a paper requires a live download;
  it is not cached for offline use, even for papers you've opened before.
- The **Inbox and Discover feeds**, and starting or continuing a Learning Card
  session after the already-open card has been rated.
- **Ask and chat** (cross-paper and per-paper RAG), and any other action
  that calls the language model — discovery, summarizing, entity
  extraction, contradiction detection.
- **Writing anything other than a card review** — editing notes, saving or
  skipping papers, exporting, uploading a PDF — while offline. These
  actions are disabled until you're back online.

If you reload the app itself while offline, the last-loaded version still
opens (rather than showing the browser's own offline error page), so you can
keep reading what's already cached.

---

## Related pages

- [Home & My Day](home-my-day.md) — the library and daily-review surfaces
  that stay available offline.
- [Learning Cards](learning-cards.md) — the review flow, including how
  offline ratings sync back once you reconnect.
