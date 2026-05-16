# JARVIS RD Assistant — User Guide

This guide is for researchers using JARVIS day-to-day. If you are setting up or
operating an instance, see [Deployment](DEPLOYMENT.md) and
[Security](SECURITY.md) instead.

---

## Getting In

JARVIS uses magic links — there is no password.

1. Open the JARVIS dashboard URL your administrator shared with you.
2. On the sign-in screen, enter your email address and click **Send sign-in link**.
3. Check your inbox. You will receive an email within a few seconds containing a
   one-time link.
4. Click the link. You are now signed in for **30 days** without any further
   action required.

That is all. No password to create, remember, or reset.

---

## Day-to-Day Surfaces

### Research Feed

The Feed surfaces papers that are new to the instance — recently ingested,
recommended by the Pulse engine, or sourced from connected Zotero groups. Use it
to stay on top of the literature relevant to your work. Rating papers here trains
Pulse's recommendations for you.

### Paper Detail

Click any paper in the feed or library to open its detail view. Here you can
read the abstract and extracted metadata, chat with the paper via the RAG
assistant, view citation relationships, annotate the paper, and step through the
Analyze pipeline (download → process → summarize) for papers not yet fully
processed.

### My Day

My Day gives you a focused daily workspace: your current reading intent, a
curated short-list of papers the system thinks are relevant to that intent today,
and any due learning cards. It is a good place to start each session.

### Projects

Projects let you group papers, tasks, and notes around a specific research
question or deliverable. Each project has its own paper list and task board.
Papers can belong to multiple projects simultaneously.

### Analytics

The Analytics surface shows activity over time — papers ingested, card reviews
completed, and library growth. It is read-only and intended to give you a quick
orientation to your own research history.

### Learning Cards

JARVIS generates spaced-repetition flashcards from papers you have processed.
The Learning Cards surface presents cards due for review using a standard SRS
interval schedule. Working through due cards regularly keeps key concepts fresh
with minimal time investment.

### Offline Reading

JARVIS is a progressive web app (PWA). Install it from your browser's address
bar (look for "Install" or "Add to home screen") and your most recently viewed
papers, cards, and feed items remain accessible when you are without internet
access. Changes made offline sync back automatically when you reconnect.

---

## How Sign-In Works and Account Recovery

### The basics

- **Sign-in links are single-use and expire in 15 minutes.** If you do not click
  the link within that window, simply request a new one — it takes about 15
  seconds.
- **A session lasts 30 days.** After that, or if you clear your browser data or
  switch to a new device, you request a fresh link the same way you first signed
  in. Nothing about your account changes: your library, notes, cards, and
  projects are stored on the server and are completely unaffected by the passage
  of time or browser state.
- **Deleting the sign-in email is harmless.** The link is only for authentication;
  once you are signed in, the email has no further function. If you missed or
  deleted it, just request another.

### Why there is no password — and why that is more secure

The sign-in link is sent to your email inbox. That inbox is the trust anchor. A
"forgot password" flow on any other service does exactly the same thing: to
prove you own an account it emails you a one-time token and lets you in. The
difference is that traditional passwords add extra ways for things to go wrong —
password reuse across sites, phishing pages that harvest credentials, breaches
that expose hashed passwords. Magic links remove those attack surfaces while
keeping the same fundamental trust model: whoever controls the inbox, controls
the account.

For the technical threat model and how session tokens are protected, see
[Security — Threat Model](SECURITY.md#threat-model).

### Account recovery scenarios

| Situation | What to do |
|---|---|
| You haven't logged in for a while (session expired) | Request a new sign-in link — same as the first time. All your data is intact. |
| You switched devices or cleared browser data | Request a new sign-in link. Sessions are stored server-side; clearing browser data just removes the local cookie. |
| You didn't click the link in time | Request a fresh one. The expired link is harmless. |
| You deleted the email | Request a new sign-in link. |
| Your email inbox is temporarily inaccessible | Wait until access is restored, then request a link. No data is lost during the wait. |
| **You have permanently lost access to your email inbox** | Contact your JARVIS administrator. They can send a sign-in link directly, or — if the inbox will never be recoverable — change the email address on your account to one you control (a verified email-change flow exists for this). Your data is preserved throughout. |

The only true hard lockout is a permanently lost email inbox with no
administrator available. Plan accordingly: keep your account email on an inbox
you reliably control.
