# JARVIS RD Assistant — User Guide

> **The canonical end-user manual now lives in the site User Guide ([docs/manual/](manual/index.md)).**
> This file is kept as a minimal offline quick-reference only. For full coverage of every
> page and feature, open the deployed documentation site and navigate to **User Guide**.

---

## Quick start (offline reference)

### Signing in

JARVIS does not use passwords. Depending on how the instance is configured, the sign-in screen offers either magic-link email or API-key login first.

1. Open the JARVIS dashboard URL your administrator shared with you.
2. If magic-link login is shown, enter your email address and click **Send sign-in link**, then open the one-time link from your inbox.
3. If API-key login is shown, enter the `JARVIS_API_KEY` value from the server operator.

Single-user installs can use API-key login without SMTP. Multi-user installs need SMTP configured and tested before magic-link invites can be delivered. For full details, see the [User Guide](manual/index.md).

### Where to get help

- **Full manual:** see the User Guide section of the deployed documentation site, starting at [`docs/manual/index.md`](manual/index.md).
- **Deployment & operations:** see [`DEPLOYMENT.md`](DEPLOYMENT.md).
- **Security:** see [`SECURITY.md`](SECURITY.md).
