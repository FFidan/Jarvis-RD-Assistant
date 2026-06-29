# Manual Update Policy

This file documents the maintenance conventions for `docs/manual/`.

## Scope

`docs/manual/` is the **end-user website manual** for JARVIS RD Assistant. It is a website-only artefact: it is not part of the lean canonical operator/developer doc set, it is not listed in `scripts/check_contract_docs.py` `DOC_PATHS`, and it must not be added there. MkDocs renders it alongside the operator docs, but it is governed separately.

## Verified-against-UI header

Every page in this manual (except `index.md` and `_meta.md`) starts with:

```html
<!-- verified-against-UI: YYYY-MM-DD | routes: /route1, /route2 -->
```

- `YYYY-MM-DD` — the date on which a human or agent last verified the page content against the live UI.
- `routes` — the React Router routes the page covers (taken from the verified IA map).

**When the UI changes:** update the page content to match, then bump the date to today. Do not update the date without also verifying the content.

## Screenshot placeholders

Real screenshots are expensive to maintain and go stale quickly. Instead, pages use:

```html
<!-- screenshot: <description of what the screenshot should show> -->
```

When adding a real screenshot later:

1. Place the image under `docs/manual/screenshots/`.
2. Replace the comment with a standard Markdown image reference: `![Description](screenshots/filename.png)`.
3. Bump the page's verified-against-UI date.

Do not commit binary screenshot files to the repository without explicit approval from the maintainer.

## What belongs here vs. in operator docs

| Topic | Location |
|-------|----------|
| End-user UI workflows, navigation, daily use | `docs/manual/` (this directory) |
| Installation, Docker, environment variables, TLS | `docs/DEPLOYMENT.md` |
| Hardening, secrets, residual risks | `docs/SECURITY.md` |
| API contracts, service boundaries, migrations | `docs/contracts/`, `docs/ARCHITECTURE.md` |
| Engineering standards, testing, linting | `docs/ENGINEERING_STANDARDS.md` |

## Adding new pages

1. Create `docs/manual/<feature>.md` with the verified-against-UI header.
2. Add the page to the `nav` section of `mkdocs.yml` under `User Guide:`.
3. Add a one-line entry to the table in `docs/manual/index.md`.
4. Do **not** add the path to `DOC_PATHS` in `scripts/check_contract_docs.py`.

## Groundedness rule

Every statement about the UI must trace to the verified frontend IA map (React routes, sidebar nav groups, component names). Do not describe UI elements that do not exist in the current implementation. If an area is in active development, add:

> _This area is evolving; verified against the UI on YYYY-MM-DD._
