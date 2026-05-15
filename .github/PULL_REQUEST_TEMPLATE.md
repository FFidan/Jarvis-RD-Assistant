## Summary

<!-- One-paragraph description of what this PR does and why. -->

Closes #

## Type of change

- [ ] fix — bug fix (non-breaking)
- [ ] feat — new feature (non-breaking)
- [ ] docs — documentation only
- [ ] refactor — no behaviour change
- [ ] test — adding or fixing tests
- [ ] chore — build, CI, tooling, dependencies

## Testing done

- [ ] `uv run ruff check services/ libs/ scripts/`
- [ ] `uv run pytest`
- [ ] `npm --prefix frontend run lint`
- [ ] `npm --prefix frontend run test -- --run`
- [ ] `npm --prefix frontend run build` (tsc-strict, no errors)
- [ ] Manual smoke test against the relevant running workflow (describe below)

<!-- Describe any manual steps, edge cases tested, or workflows exercised. -->

## Security checklist

- [ ] Cross-user isolation considered — user-scoped queries filter by `current_user_id`
- [ ] No new `current_user_id_or_none` usage on user-data routes
- [ ] No secrets, tokens, or PII written to logs
- [ ] Admin-only endpoints gated by `require_admin` / `AdminOnlyRoute`

## Migration notes

<!-- List any new DB migrations (e.g. `migrations/0xx_...sql`) and confirm they are backwards-compatible or coordinated with a deploy window. Delete if none. -->
