# Changelog

All notable changes to JARVIS RD Assistant are documented in this file.
This project adheres to [Semantic Versioning](https://semver.org/).

**Release-history note:** This changelog is retrospective. The repository remained private through the pre-v1.0.0 tags; all entries before v1.0.0 describe private development and hardening milestones.
Wording such as "public-ready", "public-readiness", or "public-launch groundwork" in older entries means preparation for the v1.0.0 public launch, not earlier public availability.
Historical sharing language describes the behavior of the release in which it
appears. The current contract is the [Source-aware paper
visibility](docs/SECURITY.md#source-aware-paper-visibility) matrix; older
references to a globally shared corpus must not be read as current behavior.

## v1.2.3 (2026-08-02)

This release closes the gap between what the product says and what it does.
Several features were documented, offered in the interface, or described in the
manual while behaving differently in practice; each is now either true or
honestly described.

### Added

- **Every documented recovery procedure is a command you can run.** Restoring an
  older unsigned backup on the same host, checking a restore's progress, and
  preparing an off-site restore request no longer require assembling container
  commands by hand. Accepting an unverified backup still requires typing the
  acceptance phrase at a prompt, and an off-site set is refused outright.
  Checking progress and preparing a request work while the stack is stopped;
  restoring replays into a running database, and says so instead of failing part
  way through.
- **The models each provider actually offers.** Provider configuration lists the
  models the provider reports, with an indication of how fresh that list is,
  rather than only a fixed built-in set. Vendor-namespaced identifiers from routers and
  self-hosted endpoints are accepted, and a provider that fails to answer is
  retried at a paced interval rather than on every request.
- **The citation graph opens what it shows.** Selecting a paper opens it;
  selecting a reference the library does not hold shows what is known about it.
- **A durable second copy of the signed-restore requirement**, so an update
  cannot silently return an installation to accepting unauthenticated backups.

### Fixed

- **Backups that can actually restore.** A backup is refused when its encryption
  key is absent, when its manifest cannot gate a restore, and when a database
  dump is incomplete. Retention can no longer delete every restore point, a
  sweep reports what it really did, and restoring a backup whose database
  version cannot be checked requires an explicit acknowledgement. Vector and
  off-site capture are reported honestly without blocking recovery.
- **Background work that reports the truth.** Jobs abandoned by an interrupted
  worker no longer sit as running forever: they are marked failed with an
  instruction to start them again. Job outcomes reflect what actually happened, advisory
  waits and batch sizes are bounded, duplicate scheduling is prevented, and
  periodic work stays on schedule across restarts.
- **Account deletion revokes that user's sessions** without disturbing anyone
  else's, and a departed user's stored vectors and rows are verified to agree
  after a purge. Service impersonation is scoped to the address that may use it.
- **Uninstall shows every removal it will perform**, including a directory
  outside the installation folder, keeps the backup offer when removing
  everything, and reports whether the final removal succeeded.
- **Setup verifies the installed command can be found**, and an interrupted
  install's staging folder — which can hold credential copies — is moved aside
  rather than deleted, and only when its owner is provably gone.
- **A failed update always explains how to roll back**, even when it cannot
  record its own progress.
- **Local uploads are identified by their full content**, so distinct documents
  are no longer treated as the same paper.
- **Automation settings report partial saves honestly**, and a skipped download
  is no longer shown as a failed step.

### Changed

- **Git is no longer part of the supported operations path.** The manual
  describes product commands throughout; repair-only fallbacks are labelled as
  such. Misuse of a command now names the correct invocation.
- **Dependencies updated within their supported ranges**, including the machine
  learning stack, and the hosted checks now run on a supported Node release. Two
  vulnerability exceptions in the Python dependency scan expired with these
  updates and were removed, so that scan runs with no exceptions at all.
- **The upload area states the size limit it enforces.** Single-file uploads
  accept up to 50 MB; whole-folder imports are unchanged.
- **A provider whose host resolves to a private address can be allowed
  deliberately**, through a setting listing the hostnames permitted, rather than
  by disabling the protection.
- The first-run tour no longer offers the topic step to signed-in users who
  cannot act on it.
- Documentation corrected where it described behavior inaccurately: cloud
  provider support, the knowledge graph controls, and how the scheduler treats a
  catch-up run that coincides with an interval run.

## v1.2.2 (2026-07-31)

### Fixed
- **Content derived from a replaced source document.** Promoting a paper to
  shared visibility now discards the processed content derived from the source
  document it replaces, and reclaims the stored files and vectors that went with
  it. Paper excerpts, stored PDFs and page images are served only when a current
  stored record exists, and discovery results and similar-paper suggestions apply
  the same rule. The same check now also gates a paper's highlights and its
  Zotero highlight export, which report the paper as unavailable while no stored
  file backs it. Discovery cards bind only to papers with shared visibility.
  Page images are pruned when a paper is reprocessed, so a replacement document
  with fewer pages no longer leaves the previous document's remaining pages
  viewable. Replacement and cleanup are ordered so a delayed cleanup cannot
  remove files or search vectors belonging to a newer source. Summaries,
  extractions, entities, relationships, contradictions and related-paper
  evidence from an earlier source no longer satisfy current completion checks
  or appear as current evidence. User notes, spatial highlights and flashcards
  are retained and visibly marked as stale instead of silently appearing on the
  new document. Cards from the earlier source are excluded from study and Anki
  export until regenerated, and stale highlights are not sent to Zotero.
- **Account data export.** Raw account exports retain owned highlights and
  contradiction records from both current and earlier source documents.
  Operational and Markdown views continue to exclude stale machine-generated
  evidence, while retained notes and flashcards are visibly identified as stale.
- **Contradiction and consensus privacy.** Holding the same papers as another
  user no longer reveals that user's contradiction results or their consensus
  assessment of those papers. Each account's scan now records and returns its
  own rows: a migration adds the owner to the contradiction uniqueness key, so a
  second account scanning the same evidence keeps a row of its own instead of
  colliding with the first.
- **Flashcard generation.** Card generation reads only the requesting user's own
  paper summary, and a batch failure reports a sanitized message rather than raw
  exception text.
- **Zotero sync.** Attachments, notes and annotations no longer create
  placeholder papers, an import with no PDF no longer queues analysis that cannot
  run, and each sync reports permanent parse failures, temporary import failures,
  exhausted analysis-scheduling retries and work deferred by the per-sync cap.
  An incomplete poll now reports a partial outcome rather than unqualified
  success, while a complete poll reports success only when every selected item
  is resolved and any library-cursor advance is stored. Scheduling retries remain
  bounded so a permanently unschedulable item cannot hold back the rest of the
  library.
- **Scheduled discovery.** Each topic is searched with its configured query terms
  rather than its name alone.
- **Answers across your library.** Papers whose stored content is no longer
  available are now set aside before an answer chooses which papers to draw on,
  rather than afterwards. They no longer take a place from a relevant paper, and
  a question whose closest matches happen to be unavailable is answered from the
  remaining ones instead of reporting that nothing was found. Similar-paper
  suggestions, discovery results and citation graphs now apply their visibility
  and stored-content checks before result limits, so unavailable candidates no
  longer leave avoidable gaps when eligible results remain.
- **Pulse relevance.** A thumbs-down now hides that paper from future decks for
  60 days at the deck sizes people actually use. Decks may therefore look
  different for anyone with recent thumbs-down history.
- **Long-running batches.** A cancelled batch reports that it was cancelled and
  how many items were completed, skipped, failed or remain. A batch with skipped,
  failed, blocked or unprocessed work reports a partial outcome instead of
  success.
- **Consensus counts.** Claim topics written in any script now cluster by their
  actual text instead of unrelated topics collapsing together, and a consensus
  view built from a truncated evidence set says so.
- **Saving search results.** If saving fails part-way through a multi-source
  search, the response identifies which results were saved and which failed
  instead of discarding the report.
- **Shared-paper processing.** Rebuilding a shared paper's derived content now
  requires holding that paper in your library. Concurrent synchronous processing
  of the same paper also waits without occupying the database connections other
  requests need.
- **Related papers.** Background-generated related-paper suggestions now draw
  only from shared papers. A private paper may therefore have fewer automatically
  generated related papers.
- **Papers whose source document is replaced.** When a source moves to a new
  revision, its excerpts, page images and search content are re-derived from the
  new document instead of continuing to serve the old one. Affected papers are
  reprocessed, which can take time on a self-hosted installation.
- **Startup and validation.** The schema-floor guard is enforced when the
  migrations directory is absent, Postgres connection credentials are
  percent-encoded so that passwords and names containing special characters
  connect correctly, and database hosts and ports are validated separately
  before a connection is attempted. Docker service names, DNS names, IPv4 and
  bracketed IPv6 remain accepted; empty or delimited hosts, malformed or
  unbracketed IPv6, and non-decimal or out-of-range ports fail at startup instead
  of altering the connection target. Request fields bounded to the width of the
  column that stores them — an over-length external identifier, tracked-author
  identifier, topic category or nudge schedule — now return a validation error
  instead of a server error. An instance starting while another instance holds
  the migration lock now verifies the resulting schema floor before serving.
- **Installer and updater health checks.** Setup and updates now keep waiting
  through a service's recoverable starting or unhealthy states for the stated
  timeout instead of failing on the first unhealthy sample. An update records
  its new application version only after every required service reaches an
  acceptable state; a running service without a healthcheck is reported as
  unverified rather than healthy. Setup records the application image version
  it installs, and uninstall derives that version from checkout metadata for
  older `.env` files that predate the pin, so application-image teardown remains
  complete across both new and upgraded installations.
- **Updates from supported releases.** Installations on v1.1.3, v1.2.0 or
  v1.2.1 load the v1.2.2 lifecycle command with a one-time bootstrap, so the
  target command recognizes the current backup and recovery state before the
  checkout advances. Before a data-changing migration, the updater creates and
  authenticates a complete restore point containing both databases, uploaded
  PDFs and data-coupled secrets.
- **Isolated install validation.** Clean-install and upgrade checks use an
  explicit Compose project, refuse a project name that already owns resources,
  and remove only that project's containers, volumes and networks. Teardown
  also reports a failure if any owned resource remains.

### Upgrade note
- This release includes four automatic migrations (0107 through 0110). They
  apply on startup and need no operator action. Migration 0110 preserves all
  existing contradiction records, including legacy rows without an owner, while
  requiring new contradiction writes to carry an owner. As with any migration,
  rolling the code back to an earlier version afterwards requires a matching
  database restore.

### Changed
- Corrected operator and user documentation to match current behavior. The
  headless restore request in the recovery runbook now carries the identity
  fields the restore entrypoint requires, so the documented procedure completes
  instead of being refused. Install guidance now derives its default models and
  27–54 GB disk range from the same selectors and calculator used by setup, and
  distinguishes the manual environment-template fallback from tier-selected
  installation.

## v1.2.1 (2026-07-24)

### Fixed
- **Access-control hardening.** Saving a paper by identifier, and de-duplicating
  a synced Zotero item by DOI, no longer attach another user's private paper, and
  citation and metadata refreshes no longer promote or overwrite an existing
  paper. A batch save can no longer claim an identifier in a namespace reserved
  for local uploads or Zotero sync, so it cannot pre-seed a row that a later
  genuine import would attach. Single-paper question-answering, summary
  generation, tracked-author updates, and the knowledge-graph views now
  consistently scope to the requesting user.
- **Scheduled discovery on a fresh install.** Automatic paper discovery again
  runs on an install that has no configured topics.
- **Background job reliability.** Batch jobs report a sanitized error code
  instead of a raw exception message, Zotero sync advances past a permanently
  malformed item instead of stalling, a fractional auto-fetch interval is
  honored, and a users-table read failure is reported distinctly from a
  genuinely empty active-user set. A Zotero sync whose cursor fails to persist
  now reports that instead of implying a durable advance.
- **Installer and lifecycle scripts.** Secret and registry writes are atomic
  across filesystems, the off-host upload grant is written with race-safe
  permissions, and the wrapper install derives its per-service network
  addresses.

### Changed
- Consolidated shared configuration loading, secret-file resolution, safe-path
  handling, and background-task registration across services, with no change in
  behavior.
- Updated bundled frontend and documentation dependencies to their latest
  available minor and patch releases.

## v1.2.0 (2026-07-23)

### Added
- **Whole-library processing.** An admin can queue eligible papers for download,
  analysis, and summary generation in one job, with per-paper progress and a
  partial result when some papers fail or are skipped.
- **Paper knowledge export.** A paper can be exported as Markdown with its
  summary, notes, cards, structured extractions, and BibTeX citation.
- **Scheduled discovery.** Each enabled source checks a rolling seven-day
  window for every configured topic, and administrators can opt in to automatic
  summaries for discovered papers.
- **Ranking-model status.** Pulse reports whether its learned ranking model is
  active instead of leaving the operator to infer it from results.
- **Explicit instance ownership.** Upgrades with one live administrator assign
  that account automatically; ambiguous multi-admin upgrades provide a host
  repair command, and database-managed owners can transfer safely in Admin.

### Changed
- **Safer lifecycle operations.** Backup manifests bind every archive in a
  restore point; update transactions resume after interruption; uninstall is
  scoped to the registered Compose project; and recovery points are verified
  before a data-changing update proceeds.
- **Clearer family access.** Remote setup and sign-in require a verified named
  HTTPS address. Plain LAN HTTP exposes only `/health/jarvis`. Guided Tailscale
  setup can install the client with explicit consent on supported Linux hosts;
  private HTTPS, Cloudflare Tunnel, and Let's Encrypt paths report only what
  their checks prove. Cloudflare also has a non-interactive token-file path, so
  its credential never needs to appear in shell arguments. Multi-user installs
  can use privately shared one-time links when SMTP is not configured. A failed
  access-route change now restores and verifies the previous live dashboard and
  JARVIS-owned edge, not only its configuration file.
- **Hardware-aware defaults.** NVIDIA acceleration is selected when its runtime
  is ready, AMD uses ROCm only when the required device is available, and other
  AMD or Intel hosts stay on the supported CPU path unless Vulkan is selected
  explicitly.
- **Complete disaster-recovery sets.** Current restore points include the PDF
  object store and exactly the three keys coupled to restored data. Restores
  revoke transient sign-in state, preserve durable identities, rotate vector
  visibility state, and quarantine off-host integration credentials until an
  authenticated operator reviews them.
- **Source-aware paper visibility.** Only papers promoted by trusted server
  adapters are public. Local uploads, client-supplied batches, personal or group
  Zotero imports, unknown provenance, and ambiguous legacy Zotero rows remain
  private unless explicitly added to a user's library. Feed, graph, citation,
  summary, vector-search, and Ask paths use the same persisted rule.
- **Audited frontend toolchain.** Transitive YAML and glob parsers are locked to
  patched releases, and the existing Security workflow rejects future
  high-severity npm audit findings.

### Fixed
- **Shared papers survive user deletion.** Removing a paper from one person's
  library no longer removes canonical data or another person's work.
- **Embedding repair covers stale and missing vectors.** Existing chunks are
  reconciled without downloading and parsing the PDF again.
- **Partial jobs no longer read as complete.** The Jobs panel labels partial
  outcomes separately and keeps their completed and failed counts visible.
- **Cross-reference visibility matches Ask.** Summaries and cross-paper
  retrieval now use the same persisted public-or-caller-library rule.

## v1.1.3 (2026-07-19)

A maintenance release for safer installation and day-to-day operation.

### Added
- **`jarvis-research` operations command.** Installs can be checked, started,
  stopped, repaired, updated, and uninstalled from any directory. Updates stage
  images before advancing the checkout, require a verified restore point before
  a data-changing migration, and resume from an interrupted phase.
- **Contained uninstall.** Four explicit tiers cover stop, application images,
  data, and full purge. Destructive tiers enumerate their targets, require typed
  confirmation, and offer to export the backup encryption key first.

### Changed
- **Setup re-runs preserve local configuration.** Existing environment values,
  operator additions, secrets, data, and user-owned Compose overrides are kept;
  newly required keys are added without rebuilding the file from scratch.
- **Access output matches the deployed route.** LAN mode prints plain HTTP,
  local HTTPS uses its own port, a named private HTTPS origin can be configured,
  and Let's Encrypt success waits for a working certificate endpoint.
- **Hardware fallback is explicit.** NVIDIA runtime checks, numeric GPU device
  groups, CPU fallback, and disk and port preflight checks fail with actionable
  guidance instead of leaving a half-started install.

### Fixed
- Setup tokens travel in URL fragments and production refuses first-admin
  creation when the token is missing.
- Admin invites return a manual one-time link when email delivery is unavailable.
- Passkeys are not offered for numeric IP origins, and authentication secrets
  are refused over non-loopback plaintext HTTP.
- Citation verification rejects references to sources that were never supplied;
  card generation preserves literal braces in paper text; and Pulse accepts
  structured tracked-author identifiers.

## v1.1.2 (2026-07-16)

A patch release closing a set of security, data-safety, reliability, and truthfulness issues found in a post-v1.1.1 audit. Unlike v1.1.1, this release changes application code, so the `:1.1.2` images differ from `:1.1.1`.

### Security
- **Owner-override is bound to a trusted proxy.** Internal owner-override now requires a trusted numeric proxy, and the browser-facing proxy strips any client-supplied owner header, so a relayed browser request can no longer act as a different user.
- **Telegram pairing is private-chat only.** Pairing and authenticated bot actions are restricted to private chats, and stale group or supergroup pairings created before this change are purged — closing a path that could deliver a user's private content to a group chat.
- **Logout is final.** Signing out can no longer leave the session cookie re-issuable, and the session cookie now carries a correct absolute expiry.
- **Passkey sign-in works again** in same-origin and default localhost installs.

### Added
- **In-browser recovery upload.** The staged restore flow gains an in-browser uploader that works across every access mode (localhost, LAN, tunnel, and domain).

### Fixed
- **Restore is safe by default.** No destructive database swap proceeds unless the secrets archive and a fresh safety backup are both verified first, and every post-restore failure clearly flags that manual steps are required.
- **Account email changes no longer deadlock** the connection pool under load.
- **Offline review replays can't rewind scheduling** — recorded review history stays in chronological order.
- **Failed loads show an error, not an empty page.** Ask, Discover, and the flashcard views now surface a retryable error state instead of masquerading as empty or "not set up yet", and a valid session is restored on a new browser tab instead of forcing a re-login.
- **Citations are verified against the cited paper**, not merely any retrieved one.
- **Truncated Telegram messages stay well-formed** — a shortened message no longer fails to send because a formatting tag was left open.
- **Cancelled background work cleans up.** A cancelled job releases its resources and is no longer recorded as a failure, and one cancelled health check no longer disrupts concurrent ones.
- **A pending email change no longer blocks the login link.**
- **Backups report honestly.** A backup skipped during a maintenance window is reported as skipped rather than failed; the retention form no longer saves from an unloaded state; and the restore schema-compatibility floor, the upload size limit, and the version and proxy documentation are corrected.
- **Clearer install and update diagnostics.** `update.sh` lists only the rollback steps for the services that actually failed, and `setup.sh` rejects an option given without its value.

### Upgrade notes
- If you paired the Telegram bot from a group or supergroup chat, that pairing is removed on upgrade; re-pair from a private 1:1 chat with the bot (Settings → Integrations) to keep receiving scheduled updates.

## v1.1.1 (2026-07-13)

A patch release that repairs install and update reliability on the prebuilt-image path and fixes the first-run smoke checks. The application and its container images are unchanged from v1.1.0; only the installer, updater, and CI scripts changed, so the `:1.1.1` images are functionally identical to `:1.1.0`.

### Fixed
- **Non-interactive install no longer aborts at startup.** The non-interactive bootstrap now generates the Langfuse key material before starting the stack, so a fresh install no longer fails at `docker compose up` with a missing-secret error.
- **Disk preflight honours the selected accelerator.** An explicit `--gpu` now drives the disk estimate, so a CUDA install no longer under-budgets (which risked running out of space mid-pull) and a CPU install on an NVIDIA host no longer over-budgets and blocks.
- **Re-running against an existing `.env` regenerates missing secrets**, so a second `./setup.sh` on a partially provisioned host no longer dead-ends at startup.
- **AMD (ROCm) Ollama updates correctly.** The ROCm image is now pinned and `update.sh` compares against it, so AMD hosts are no longer reported as perpetually out of date.
- **Shared directories stay readable on non-root hosts.** When ownership cannot be handed to the container user, the directories keep world read-and-traverse permission so the container can still read them.
- **Clearer install diagnostics.** A flag passed without its value now reports which flag needs an argument instead of a raw shell error, and `update.sh` lists third-party and application services under the rollback command that applies to each.
- **First-run smoke reports honestly.** The clean-machine checks now read the installer's real exit code, which a pipeline had masked — so the restart-required check passes and a genuine failure can no longer be reported as success. The prerequisite check also accepts Docker Compose v2 and newer.

## v1.1.0 (2026-07-13)

The install-and-distribution release. A default `./setup.sh` now installs JARVIS by pulling prebuilt, multi-architecture container images from the project's registry instead of building them locally, which removes the multi-gigabyte PyTorch/CUDA build that could exhaust disk on a first install. This release also adds passkey sign-in, rolling sessions, and a browser-driven disaster-recovery flow, and makes disk, hardware, and status reporting honest about what a given host will actually do.

> Includes the operational-friction and status-truthfulness improvements that landed after the v1.0.4 tag (grouped under "Post-v1.0.4 improvements" below).

### Added
- **Install from prebuilt images.** The default installer pulls the four application images (plus the restore-uploader) from the container registry and brings the stack up without building, selecting a CPU or CUDA image flavor from the detected GPU. A `--build-local` flag keeps the from-source path for contributors, forks, and air-gapped installs; a failed pull stops with a clear message pointing at `--build-local` rather than silently building.
- **Passkey sign-in and device management.** Register and sign in with a platform or roaming authenticator, manage credentials from the web UI, and revoke a credential (which also ends its sessions). Passkeys require a secure-context origin; raw-IP LAN installs keep the magic-link flow.
- **Rolling sessions.** An in-use session renews across both the cookie and the database, at most once per day, and stale sessions and challenges are purged on a schedule.
- **Web-based disaster recovery.** Recover onto a fresh host entirely from the browser: upload the backup archives and one-time key, trigger an inbox restore, and let the stack reconcile secrets, the database role, and services automatically — no terminal steps after `./setup.sh`. A guided recovery view reports progress throughout.
- **Catalog-driven disk preflight** measured at the Docker data root, with a `--skip-disk-check` escape and a read-only `--check`.
- **GPU vendor and VRAM detection** across NVIDIA, AMD, and Intel hosts, surfaced in the setup wizard and the status API; experimental ROCm and Vulkan compose overlays for AMD and Intel.
- **Self-explanatory access modes.** The setup chooser presents localhost, LAN, Cloudflare Tunnel, and Let's Encrypt with their capability consequences, and derives a canonical application URL.
- **Double-clickable launchers** for macOS, Linux, and Windows, and a hardware-validation helper for contributors certifying a new GPU.
- **Admin storage-usage card** on the System Health page, and an estimate of reclaimable disk space when removing an installed model.
- **Documentation**: a hardware support matrix, consolidated requirements, install troubleshooting, and new user-guide pages for access modes, passkeys, and backup & restore.

### Changed
- **Restore is now staged and atomic.** A restore loads into a staging database and swaps it in with a rename, self-healing back to the original data if any step fails, instead of dropping the live database first. Older backups migrate forward automatically; scheduled backups stand down while a restore runs, and the restore's progress poll stays authorized while the database is swapped out.
- **Session minting is unified** across all entry points, and sign-in help points at the admin-link recovery flow when email delivery is not configured.
- **Continuous integration** publishes multi-architecture images on release tags, enforces a measured disk budget, runs a release-blocking cold-install check, and promotes the mocked end-to-end suite to a required check.
- Bumped the Ollama image and the docling, FastAPI, and frontend dependency pins.

### Fixed
- Require HTTPS for a configured public passkey origin.
- Harden restore recovery-target validation and preserve host-authoritative service keys during an off-host restore.
- Pause background writers during maintenance and reconcile the schema forward on resume.

### Removed
- The `JARVIS_TUNNEL_ACK_ZT_CONFIGURED` environment hand-edit is gone; Cloudflare Tunnel is now acknowledged in the setup flow (or with `--tunnel-ack`). Pre-1.1 `.env` files carrying it are ignored harmlessly.

### Post-v1.0.4 improvements
_These landed after the v1.0.4 tag and are included in v1.1.0._
- The application version is reported by the API and health endpoints, shown in the UI, and recorded in backup manifests, distinguishing a redeployed server from a stale browser tab.
- Restore reports its maintenance state rather than generic connection errors, exposes its current phase and manual steps, and enforces an upper bound on its own runtime; admins can delete individual restore points behind a typed confirmation and set a keep-last-N / maximum-age retention policy.
- The sign-in page explains the magic-link cooldown and reports an unreachable email relay instead of failing silently; suppressed send failures are logged without storing addresses.
- Custom OpenAI-compatible model endpoints are validated against private and reserved address ranges before use.
- The model settings page keeps only the per-role selectors; detected hardware, the serving backend, and the recommended model move to a compact runtime summary on the System Health page. The recommended model for the 24–48 GB tier is qwen3:14b.
- Unhandled server errors are recorded in the event log, deduplicated, alongside backup lifecycle events.
- Consensus and contradiction scans explain empty results; cross-references again include shared-corpus papers; an unknown model provider returns HTTP 400 rather than 500; an empty SMTP value is treated as unset. soupsieve updated to 2.8.4 (GHSA-2wc2-fm75-p42x, GHSA-836r-79rf-4m37).

## v1.0.4 (2026-07-08)

A focused maintenance and polish release that stabilizes magic-link email verification, simplifies Advanced settings to prevent duplicate model assignment, and introduces guided preflight prerequisite installation.

### Fixed
- **Magic-link verification loop.** The authentication verify page and routing engine now prevent infinite loop states and UI flickering on authenticated remounts and token reuse, while preserving secure cross-user cache purging.

### Changed
- **AI settings simplification.** Conflicting and redundant model dropdown selectors and backend apply/reset actions are removed from the collapsible Advanced settings panel. Model selection is now managed exclusively by the primary Quick and Main model cards at the top.

### Added
- **Guided prerequisite installer.** Bumps setup script preflight checks to plan, prompt, and install Docker and OpenSSL on supported hosts (`apt` for Ubuntu/Debian, `brew` for macOS) when run with `--install-prereqs` or interactively.

## v1.0.3 (2026-07-06) — Provider routing, sign-in resilience, and consensus trust hardening

A maintenance release that expands optional cloud-provider setup without changing the local-first default, improves first-hour reliability, and tightens multi-user data boundaries around Consensus evidence.

### Added
- **Provider setup and routing.** Settings now has a registry-driven provider setup flow for OpenAI, Anthropic, Google Gemini, OpenRouter, DeepSeek, Mistral, Kimi/Moonshot, Z.ai/GLM, and Custom OpenAI-compatible endpoints. Cloud providers remain optional and admin-wide; local-only installs continue to work without API keys.

### Changed
- **AI settings clarity.** Model selection keeps local models first while showing cloud entries only when their provider prerequisites are met. Documentation now distinguishes local-first defaults from whole-application offline guarantees.
- **Providers & Routing navigation.** The provider settings page, settings rail, and public manual now use the same label and show an explicit error state when provider metadata cannot load.
- **Consensus scan feedback.** The Consensus page now reflects scan progress and completed zero-result or failed scan outcomes without asking users to reload manually.

### Security
- **Provider administration boundary.** Provider metadata and provider connection tests now require an administrator session, matching the admin-wide storage model for provider credentials.
- **Custom endpoint validation.** Custom OpenAI-compatible endpoints are validated before test and routing delivery, including resolved-address checks for implicit loopback, link-local, multicast, unspecified, and metadata-service addresses while still allowing explicitly configured localhost endpoints.
- **Consensus evidence isolation.** Persisted contradiction and consensus reads now require both evidence papers to belong to the caller's library, and candidate finding loads are scoped to the caller's library during scans.

### Fixed
- **Magic-link verification.** Transient backend/proxy errors during magic-link verification are retried before showing a terminal sign-in failure, while invalid or expired tokens still fail immediately.

## v1.0.2 (2026-07-05) — First-hour setup clarity, account export, and release alignment

A focused maintenance release that keeps the local-first defaults unchanged while improving first-run clarity, account data access, and release metadata consistency.

### Changed
- **Account data export in Settings.** Signed-in users can download their own structured account data from Settings → Account. The export is scoped to the user’s private activity and output; it is not a shared scholarly corpus export or a PDF backup.
- **Clearer library preparation from Home.** Admin users can trigger useful library-preparation actions from Home, with job tracking for returned background work and honest feedback when no action is available.
- **Setup and deployment wording.** Docs emphasize `setup.sh` as the preferred install path, keep cloud keys optional, and clarify the boundary between a signed-in user’s export and the shared scholarly corpus.

### Fixed
- **Ask answer hygiene.** Streaming Ask responses now validate the assembled visible answer before emitting sources or completion metadata. Empty or non-final model output returns a friendly retryable error instead of presenting unsupported content.
- **Local model-route restoration.** Saved local model choices continue to route through the local Ollama backend even after an operator temporarily exercises another backend for a model alias. No model, backend, reranker, embedding, or cloud default changes in this release.
- **SMTP configuration documentation.** Public security and residual-risk docs now match the settings behavior: explicit empty-string SMTP secrets are rejected, while intentionally unset SMTP remains supported for single-user/API-key deployments.
- **Version metadata drift.** Package metadata, compose image fallbacks, citation metadata, roadmap wording, and the frontend lockfile are aligned for v1.0.2.


## v1.0.1 (2026-07-03) — Maintenance: first-run reliability, security hardening, and module decomposition

A maintenance release focused on first-run reliability, security hardening, and backend code structure. There are no user-facing feature changes and public API behavior is unchanged.

### Fixed
- **First-run startup for non-root service containers.** Generated secret files are now readable by the non-root users that the service containers run as, so a fresh setup or `make up` no longer fails when a container cannot read its bind-mounted secret. The secrets directory keeps owner-only access, so host-side confidentiality is unchanged.
- **Setup script robustness.** Removed an unset-variable error that could abort the setup script on the non-tunnel access paths.
- **Scheduled trigger authorization.** The scheduled Pulse generation endpoint again accepts the API-key caller used by automated triggers.
- **Console warnings.** Fixed an invalid nested-button structure in the deck browser and a controlled/uncontrolled tooltip toggle in the chat empty state.

### Security
- **Traversal-safe file serving.** Centralized path validation for served PDFs, page snapshots, and backup archives behind a single traversal-safe path-join helper.
- **Keyed provider-key fingerprint.** The in-process fingerprint used to detect provider-key delivery changes is now a keyed, computationally hardened hash rather than a plain digest.
- **Static-analysis alerts.** All previously open code-scanning alerts are resolved.

### Changed
- **Module decomposition.** Several large backend modules were split into focused modules with unchanged public behavior: the application entrypoint's model reconciler, the system readiness and capabilities routers, the Zotero integration (configuration, push, highlights, polling, and job handlers), and the LiteLLM HTTP helpers.
- **Complexity reductions.** High-complexity functions across the summarization, search, retrieval-streaming, model-lifecycle, system, and Zotero paths were decomposed into smaller helpers, and duplicated logic was consolidated. The Python complexity budget and the frontend lint budget were both ratcheted down to the new lower levels.
- **Dependencies.** Raised the docling version ceiling and picked up a group of frontend patch and minor dependency updates.

## v1.0.0 (2026-06-29) — First stable public-launch release: consensus, PDF annotation, Zotero sync, restore, and multi-user hardening

First stable release and public-launch baseline. Earlier tags were private development and hardening milestones; v1.0.0 is the first version intended for public availability. It adds a cross-paper consensus view, an in-PDF reader with spatial highlights that sync to Zotero, guided one-click backup restore, and completes per-user isolation and restore-safety hardening across the application.

### Added
- **Consensus view.** A new page shows where the papers in your library agree or disagree on shared claims extracted by the contradiction pipeline: an "Agreement by claim" chart with per-claim Supports and Opposes counts, and expandable evidence showing the assessed quote from each compared paper. A consensus scan can be queued on demand, with a guided empty state for the first run.
- **In-PDF reader with spatial highlights.** Source PDFs render directly on the paper detail page. Select text to create a colour-coded highlight with an optional note; highlights can be edited and deleted and are anchored to their exact position on the page.
- **Highlight export to Zotero.** A paper's not-yet-synced in-app and in-PDF highlights can be pushed to a linked Zotero library as annotations; already-synced highlights are skipped.
- **One-click restore.** The admin Backups panel restores a point-in-time backup through a guided, typed-confirmation flow. Restore points carry compatibility metadata, and an off-host restore inbox supports recovery onto a fresh host.
- **Admin account recovery.** An administrator can restore a soft-deleted user, and a usable sign-in link is surfaced when email delivery is unavailable.

### Changed
- **Per-user Zotero.** A paper's Zotero item, attachment, citation key, and sync state are now scoped to each user through a per-user link table, so collaborators no longer share or overwrite one another's Zotero links. Citation keys and the library Zotero indicator are likewise scoped per user.
- **Restore safety.** A sentinel-driven maintenance mode keeps the application returning 503 while a destructive restore is unfinished or was interrupted, rather than serving partially restored data; the boot sequence validates multi-user, setup-token, and email configuration once the database is available.
- **Hybrid-search enrichment** batches paper lookups into a single query.
- **Public project notices.** The license appendix, NOTICE, AUTHORS file, citation metadata, and package metadata now name Ferhat Fidan as maintainer/copyright holder, list `jarvis-rd@limitcycle.dev` as the project contact, and disclose substantial AI-assisted development while keeping the Apache-2.0 license terms intact.

### Security
- **Per-user data isolation.** PDF and page-snapshot access for private sources is scoped to the owner, the filesystem-wide local PDF scan is restricted to administrators, and a task's parent must belong to the same project.
- **Owner sign-in and administrator safeguards.** The first administrator is persisted as the owner and resolved at API-key login; the last-administrator safeguard is serialised so it cannot be bypassed by concurrent requests.
- **Restore integrity.** A restore whose backup manifest is unreadable is rejected before any destructive step, and the restore archive is protected from the routine backup-retention prune.
- **Atomic account removal.** Account deletion and audit-log anonymisation are applied atomically.
- **Release license gate.** A pre-release check fails the build on strong-copyleft dependencies, and a `NOTICE` file documents the bundled third-party components.
- The setup token is no longer recorded in access logs, and cached identity data is cleared on session expiry and re-login on shared browsers.

### Fixed
- **Spaced-repetition reviews** synced from offline use are scheduled at their original review time rather than at sync time.
- **Cross-service Zotero push** is queued reliably when a paper is linked from a project.
- **Telegram reminder reloads** surface failures instead of silently swallowing them.
- Downloaded PDFs are written atomically to avoid corruption on retry, and highlights cascade when a user is deleted.
- Only newly imported library papers are queued for analysis during a Zotero poll, avoiding redundant reprocessing.
- Clickable cards are keyboard-operable.

## v0.9.2 (2026-06-25) — Hardened structured AI output, setup & account safeguards, permissive PDF rendering

### Added
- **Effective-configuration view.** A new admin endpoint reports the resolved model roles, transport, and structured-output enforcement state, so a misconfigured model default can be diagnosed at a glance.
- **Third-party NOTICE.** A `NOTICE` file at the repository root documents the bundled third-party components (LGPL, MPL, NVIDIA, and the PDFium licenses).
- **Nightly model smoke check.** A scheduled workflow exercises each AI pipeline against a live model, with an offline cassette fallback that runs on every check, so a structured-output regression is caught early; an active alert is raised when a run produces no AI scores.

### Changed
- **Relicensed to Apache-2.0.** The project license moved from MIT to Apache-2.0; license metadata and documentation are aligned.
- **Permissive PDF rendering.** Page snapshots are rendered with pypdfium2 instead of PyMuPDF, removing the AGPL dependency; text extraction is unchanged.
- **Concurrent stack-health checks.** Health probes run in parallel with per-probe timeouts and never report a degraded or unknown dependency as healthy.
- **Plain-language relevance labels** are sourced from a single shared map across the relevance views.

### Security
- **Structured-output enforcement at the model boundary.** AI features use grammar-constrained decoding so a model can no longer return its response schema instead of a result; the daily Pulse, knowledge-graph extraction, and summaries degrade honestly rather than silently accepting malformed output.
- **Owner sign-in safeguard.** A configured owner can always create an API-key session, preventing a multi-user lockout; on multi-user deployments API-key login requires an explicit owner, and a dedicated model-signing key is required.
- **First-run setup token.** Bootstrap setup writes require a one-time token issued by the installer, closing an unauthenticated first-administrator takeover window on network-reachable deployments; the setup status stays readable so the wizard can guide the administrator.
- **Backups never archive secrets unencrypted**, and required secrets are provisioned as a prerequisite of `make up`.

### Fixed
- **Invite links when email is unconfigured.** Inviting a user without working email returns a shareable invite link instead of failing silently.
- **Admin paper-by-status counts** no longer double-count papers shared across users.
- **Citation fetching reports partial progress** instead of discarding successful fetches when one of them fails.

## v0.9.1 (2026-06-24) — Fix daily Pulse AI relevance scoring

### Fixed
- **The daily Pulse computes AI relevance scores reliably.** The shipped configuration pointed Stage-2 scoring at the small local model alias, which intermittently returns the response schema instead of a score and silently disables AI relevance scoring (every card shows "AI scoring unavailable"). Stage-2 now defaults to the larger, structured-output-capable model — matching the in-code default — with the deployment defaults, documentation, and a regression test aligned to it.

## v0.9.0 (2026-06-24) — Working daily Pulse, plain-language UI, clickable citations, citation export, and hardened backups & CI

### Added
- **Citation export.** Export or copy any paper's citation as BibTeX or RIS from the feed, the paper detail page, and the bulk-selection toolbar; bulk export concatenates the selected papers into one file. BibTeX is generated natively, without an external BibTeX runtime dependency.
- **PubMed as a discovery source.** PubMed (NCBI E-utilities) is enabled as a selectable source in Discover.
- **Restore points in the Backups panel.** The admin Backups panel groups archives into restore points (a point-in-time set across the databases, vectors, and secrets) showing completeness, encryption, size, and retention per point, with a collapsible per-file detail view.

### Security
- Pulse generation and the source-configuration listing now require an admin session.
- The "generation already in progress" response no longer includes another user's job identifier.
- Qdrant vector snapshots are now encrypted at rest with the backup key (previously the snapshot files were written unencrypted); a failed secrets archive is recorded as failed rather than skipped.
- The CI security gate adds repository secret scanning and static analysis (CodeQL), and releases flag copyleft-licensed dependencies.

### Changed
- **Plain-language interface.** Researcher-facing screens were relabeled to remove implementation jargon (for example "excerpts" instead of "chunks/passages", "all papers" instead of "corpus", and clearer source and status labels), with labels centralized so they stay consistent across the app.
- **Clickable citations.** Citations in answers link to the cited paper.
- The backup status distinguishes the last attempt from the last success, so a failed run is visible instead of being masked by an older successful archive.

### Fixed
- **The daily Pulse computes AI relevance scores again.** The per-card scoring call to the local model now returns structured output successfully; when scoring is genuinely unavailable the deck shows a single calm "basic ranking" banner instead of a red "AI scoring unavailable" message on every card (including the "why this matched" popover).
- Service health checks resolve to a status within a bounded time instead of showing "Checking services…" indefinitely; a check that cannot complete is reported as unknown rather than collapsing a real outage into "healthy".
- Ask shows a guided empty-state when no analyzed papers exist, and a failed question renders a friendly error with a Retry action instead of raw error text. Ask no longer shows that empty-state while the library check is still loading or has failed.
- Knowledge Graph nodes open a detail panel, and graph-query results render as cards rather than raw JSON; graph layout and labels are less cramped.
- The citation graph honors the current paper selection instead of fetching across the whole library.
- The Zotero polling cursor is persisted for the shared (no-user) configuration, preventing a repeated re-poll from the beginning.
- A provider API key is no longer cleared when its field loses focus while empty.
- The restore runbook references the actual Qdrant collections (`kg_entities`, `paper_chunks`) and notes the decrypt-first step for encrypted snapshots.
- The Backups panel shows a distinct loading state instead of briefly reading "backup service not running", and reports an explicit "status unavailable" state when the status check fails.

### Maintenance
- Widened the supported FastAPI range and migrated route enumeration to the version-stable API.
- Added a first-run clean-machine smoke check to CI.

## v0.8.8 (2026-06-18) — Cross-tenant data-scoping, ingestion recovery, an admin Backups panel, and the sign-in-method reframe

### Added
- An admin **Backups** panel (Admin → Backups) lists the disaster-recovery archives, downloads them, and triggers an on-demand backup; a read-only restore runbook surfaces the host restore commands without executing them.

### Security
- Deleting a paper now removes only the requesting user's library membership and that user's vectors; the shared canonical record and other users' data are preserved unless the caller is the last holder. Per-user purges likewise skip vectors still held by another user.
- The research-feed summary join is scoped to the requesting user, so another user's summary text or duplicate rows can no longer appear in a feed and the result count is no longer inflated. The pending-papers count and per-user entity paper counts are scoped the same way.

### Changed
- The access-mode toggle is relabeled **Sign-in method** and the change applies on the next status check — the previous "restart required" prompt (which never reflected a real requirement) was removed. The login gate itself is unchanged.
- The disaster-recovery backup sidecar (`postgres-backup`) now runs by default; it was previously opt-in via the `backup` compose profile. Archives are encrypted at rest with the auto-generated key. Stop the `postgres-backup` service to opt out (e.g. if you back up externally).

### Fixed
- A paper whose embedding was interrupted mid-run is no longer treated as fully processed; it is re-embedded on the next pass, while papers already fully embedded are marked so they are not reprocessed.
- Encrypted backups no longer seal the encryption key inside the archive it unlocks (the key file is excluded — keep it off-site); Qdrant snapshots stream to disk instead of buffering in memory under the sidecar's memory limit; orphaned temporary files are pruned; and the backup-interval setting is honored.
- Ask returns a 503 (non-streaming) or a degraded error frame (streaming) when the vector store is unavailable, instead of a generic 500.
- Changing your account email to one already in use returns a clear 409 instead of a 500.
- Research-feed bulk-selection actions apply across the active filter set rather than only the visible page.
- Email/SMTP readiness no longer reports green when only a partial relay configuration is present.
- The weekly summary gates on recent engagement rather than discovery date, and scheduled-job failure alerts are sent only to the owner.
- The "process PDF" action honors the force flag, so requesting a reprocess actually re-runs it.

## v0.8.7 (2026-06-17) — Email reliability, data-scoping, error handling, and feed fixes

### Security
- The weekly summary digest scopes the paper-summaries join to the requesting user, so one user's summary text can no longer appear in another user's digest.
- The knowledge-graph minimum-paper-count filter uses each user's own mention counts instead of a global cross-user counter.

### Fixed
- Magic-link send failures are recorded as a `magic_link_delivery_failed` system event and surface in the Logs Live tab instead of failing silently. Previously a relay with an unusable stored password returned success to the caller, so the sign-in screen reported the link was sent while no mail went out.
- Email/SMTP status flags a username-set-but-no-password configuration, the system readiness check reports SMTP as configured when only the database-stored relay is set, and magic-link sends use an explicit send timeout. The "Save & send test email" button reuses the stored password when the field is left blank.
- Paper text is tokenized with `disallowed_special=()`, so a PDF containing the literal `<|endoftext|>` no longer aborts chunking.
- Summarization errors preserve the underlying cause and log diagnostics; transient (timeout / 5xx) LLM failures are retried up to twice while permanent errors fail fast.
- Entity extraction uses the dynamic context budget instead of a fixed page cap.
- A Qdrant outage during Ask returns HTTP 503 on the non-streaming routes and a degraded error frame on the streaming routes, instead of being masked as "no relevant passages" or a generic 500. Genuinely empty results are unchanged.
- The Source facet filters on the Library and Trash surfaces (it previously appeared active but was ignored outside Inbox).
- The Untagged filter is implemented end-to-end; the My-Day views were migrated off the deprecated `statuses` parameter before it was retired.
- Inviting an email that belongs to a removed user returns 409 with a clear message instead of a 500.

### Changed
- The Knowledge Graph "Batch Extract Entities" empty-state action is hidden from non-admins.
- The Home "Library" metric tile was relabelled so it no longer reads as the Library page's own count.
- The sign-in screen uses neutral "if an account exists for that address" wording.
- The access-mode setting description was corrected to match what the toggle controls.
- The backup script also covers the LiteLLM database, Qdrant vectors, and the secrets directory (previously the jarvis Postgres database only).
- Langfuse is disabled cleanly with a single startup line when unconfigured, instead of logging a warning on every traced call.

### Documentation
- Documented the `litellm_salt_key` deployment secret and the encrypted-restore recipe, the `uv` toolchain prerequisite for contributors, and the `smtp.reply_to` / `smtp.from_name` settings; reconciled version strings and "Last updated" stamps.

## v0.8.6 (2026-06-15) — SMTP sender identity & dependency security

### Added
- **SMTP Reply-To and sender display name.** Two optional SMTP fields are now available in both the first-run wizard and Settings → System → Email / SMTP: a **Reply-To address** (routes user replies away from the From address) and a **Sender display name** (sets the friendly name in the From header, e.g. `JARVIS RD <login@your-domain.dev>`). Both can be set via the UI or the new `SMTP_REPLY_TO` / `SMTP_FROM_NAME` environment variables; leaving a field blank clears the stored value.
- **SMTP misconfiguration warning and in-place test send.** Settings → System → Email / SMTP now shows an amber warning banner when the effective mail relay is not deliverable (partial configuration, empty-string env vars, or no relay set). The **Save & send test email** button — previously only in the wizard — is now also available in Settings, with an optional test recipient and inline error reporting.

### Security
- Patched known CVEs in dependencies: aiohttp (3.14.1), cryptography (≥48.0.1), python-multipart (≥0.0.31), and starlette (1.3.1), raising the corresponding floors. The js-yaml dev-toolchain transitive is pinned to 4.2.0 via an npm override (GHSA-h67p-54hq-rp68).

### Changed
- Updated the project contact email to `jarvis-rd@limitcycle.dev`.

## v0.8.5 (2026-06-14) — Trustworthy, Comprehensible & Credible

A polish release focused on trust, plain language, and a clean, launch-ready
codebase. Researcher-facing screens no longer surface implementation jargon,
real settings dead-ends and false-failure states are fixed, and the project's
toolchain is brought up to date.

### Fixed
- Zotero "Test connection" reports success correctly (was a false "Failed" on a valid key).
- Access-mode status reflects the saved value and shows the exact restart command; a "restart pending" indicator persists across reloads.
- The SMTP test and magic-link delivery work on plain port-25 relays, not only STARTTLS/implicit-TLS ports.
- Pulse scoring uses a capable model and records an honest "ranked heuristically" reason when LLM scoring is unavailable.
- Project updates return real paper and open-question counts; empty project, task, and milestone names are rejected.
- Telegram review distinguishes a load error from "all caught up" instead of falsely reporting completion.
- No settings dead-ends: every hardware tier has a selectable model, and the AI-backend page guides you instead of showing a bare "no candidates".

### Clarified (plain language)
- System Health reads in plain language — labels, a verdict word, per-service consequences, and an overall summary.
- Logs filters distinguish Severity from Area, with visually distinct chips.
- A single authoritative model-settings page (advanced backend/hardware controls move behind a disclosure); plain model names, fit badges, and selector copy.
- Pulse optional signals are locked when unavailable, with each prerequisite named.
- Onboarding, login, My Day, and paper surfaces use researcher language (no internal terms like context-window internals, "RAG", routing tiers, raw cron, or env-var names on screen).

### Hardened
- The Telegram bot token is stored as a secret; the self-hoster setup scripts generate and validate every required secret through one generator.
- Infra-event uploads are bounded by streamed size; paper-summary reads are scoped to the owner.

### Docs & maintenance
- Replaced developer-rig GPU names with hardware-tier descriptors; removed internal tracking identifiers from comments and tests; neutralized key-rotation guard wording.
- Re-baselined the database schema into a single clean baseline; localized de-duplication across source plugins, the request layer, and lifecycle responses.
- Upgraded the frontend toolchain (ESLint 10, Tailwind CSS 4) and wired the end-to-end test suite into CI.

## v0.8.0 (2026-06-13) — Trustworthy & Frictionless

A reliability- and trust-focused release. The goal: a researcher with no CS
background can install JARVIS, get a correct full-coverage summary and a
trustworthy Ask answer — with the right model for their hardware actually
running — without editing a file or learning the word "num_ctx".

### Highlights

- **Install that survives a bare machine.** `./setup.sh` no longer crashes on
  hosts without PyYAML, checks Docker daemon access up front, keeps your data
  when you re-run it, and streams the first model pull so the long download is
  visible. Honest CPU/GPU speed expectations are documented up front (including
  macOS).
- **Model choices are real, or honestly pending.** Changing the main/quick model
  in Settings now actually re-routes the LLM and survives a restart. If the model
  service is briefly unavailable your choice is saved and applied automatically
  within about 30 seconds, with a clear "applying" badge — never a silent revert
  to the old model.
- **The right model out of the box.** On first run JARVIS picks the largest model
  your GPU can comfortably run (keeping the embedder resident) plus a safe
  reading window to match — no manual tuning — and tells you what it picked.
- **Long papers are read in full.** Summaries and flashcards now read 100% of a
  paper via a map-reduce pass instead of only the opening pages, with a quiet
  note showing how many passes it took. Verified quotes are only ever taken from
  text the model actually read.
- **One reading window.** The context size is a single plain-language slider
  ("Reading window") in Settings → Models that flows through the whole pipeline;
  raising it speeds up GPU analysis and is bounded to a memory-safe maximum.
- **A trust layer you can read.** Ask answers carry an honest confidence badge
  with in-app definitions, short factual answers no longer show a misleading
  "unverified" banner, and the weekly digest's theme verification is real.
- **Frictionless research flow.** You can Ask as soon as a paper is analysed
  (no Topics required), there is one "Analyze" verb everywhere, the feed
  surfaces are named consistently (Library / Discover), errors tell you what to
  do next, and the Pulse deck can be regenerated where it lives.
- **A gentler learning curve.** A new simple navigation mode shows just the daily
  essentials for first-time users (everything else one click away), and the model
  settings now read in plain language.
- **Hardening.** Card generation degrades gracefully on provider errors,
  cross-tenant vector isolation is enforced, contradiction scans de-duplicate
  concurrent runs, and several configuration values are now validated. The
  frontend build moved to Vite 8's Rust toolchain (Rolldown), which also drops
  a vulnerable build-time dependency.

### Upgrade Notes

- **Re-run `scripts/init-secrets.sh` before `docker compose up`.** This release
  adds a required `litellm_salt_key` secret; compose will not start without it.
- **Model configuration now lives in the LiteLLM admin database**, delivered via
  the model-management API rather than the YAML file. Fresh installs need no
  action; existing installs reconcile automatically on first boot. The switchable
  `smart`/`fast` aliases are no longer seeded from `litellm/config.yaml`.
- **The reading window (`num_ctx`) is now a single value** on the Settings →
  Models slider. The `LLM_SMART_NUM_CTX` environment variable remains the
  boot-time default/fallback only — you no longer keep it in sync by hand.

## v0.7.0 (2026-06-11) — Research Quality

Focused research-quality release: self-contained flashcards, more reliable AI
summaries, a smarter Ask pipeline, server-side library search, and several
mobile refinements.

### Upgrade Notes

- **LiteLLM `num_ctx` migration.** Summaries and flashcards now budget their
  prompt input to the model's context window instead of sending a fixed amount
  that silently overflowed it. If you have customised `num_ctx` in
  `litellm/config.yaml`, set the new `LLM_SMART_NUM_CTX` environment variable
  to the same value and recreate the proxy and its consumers
  (`docker compose up -d --force-recreate litellm paper_ingestion
  learning_engine`). When the variable is unset the app assumes the stock
  8192-token context.
- **New optional Ask tuning knobs.**
  `RAG_RELATIVE_SCORE_CUTOFF` (default `0.85`) gates retrieved sources by their
  relevance relative to the top-scoring result for each query facet.
  `RAG_MIN_RERANK_SCORE` sets a hard floor when the optional reranker is
  enabled. Both can be left unset to accept the defaults.

### Added

- **Library search.** The filter box in the Library and Inbox now performs a
  server-side full-text search (title, author, and abstract with stemming) when
  three or more characters are typed. Previously the box only filtered the
  already-loaded page.
- **Regenerate Summary action.** A "Regenerate Summary" button is now available
  in the paper sidebar so you can re-run the summarisation step without
  re-analysing the whole paper.
- **Calibrated confidence badge.** The Ask answer badge now reflects the
  degree of grounded support (Verified / Mostly verified / Partially verified /
  Unverified) rather than
  a simple pass/fail. A warning is shown only for low-confidence and unverified
  answers.

### Changed

- **Flashcard fronts are self-contained.** A generic-question filter prevents
  cards whose front question only makes sense with the paper in hand (e.g. "What
  is the main contribution of this paper?"). Generation prompts are rewritten to
  produce standalone questions. If no suitable cards can be generated, a clear
  message with guidance is shown rather than a synthetic fallback card.
- **Summary reliability on small models.** The output budget for AI summaries
  is raised, and the prompt input is budgeted to the model's context window via
  the new `LLM_SMART_NUM_CTX` setting (paired with LiteLLM's `num_ctx`). When
  the LLM returns a summary that does not pass quality verification, the result
  is still displayed with a "low-confidence" label instead of being silently
  replaced by an error string.
- **Cross-references are semantic-only.** Cross-reference suggestions between
  papers are now based on embedding similarity rather than keyword overlap,
  eliminating spurious links caused by shared common words.
- **Ask context carries across follow-ups.** Follow-up questions in the Ask
  workspace now include the current conversation context, enabling coherent
  multi-turn research dialogues.
- **Ask source panel wording.** Retrieved passages are now labelled "Source
  Passages" in the UI. Numeric relevance scores, model names, and internal job
  IDs have been removed from the answer view.
- **Relevance gate for retrieved sources.** Retrieved passages are filtered by
  a relative-score gate per query facet before being used to construct an answer.
  An optional reranker floor (`RAG_MIN_RERANK_SCORE`) is applied when the
  reranker is enabled.
- **Feed cards stack on mobile.** Research feed cards now stack vertically on
  narrow phone viewports. Topic rows wrap correctly, the Discover heading is
  visible, and the install-banner dismissal is persisted across page loads.

### Security / Dependencies

- The PyTorch advisory CVE-2025-3000 is triaged in the security scan
  configuration. No patched wheel is available from upstream at this time; the
  vulnerability affects PyTorch model serialisation and is not reachable via
  JARVIS's default Ollama-based inference path.

---

## v0.6.0 (2026-06-06) — Multi-user self-hosting hardening

Private hardening milestone for the later public launch. Highlights since v0.5.0: **per-user
multi-tenant isolation**, **GDPR-purge correctness**, **SMTP-SSRF + credential
encryption**, **token-only Telegram pairing** and the Telegram→REST decoupling,
**GPU/setup foolproofing**, **whole-app mobile**, and a **unified onboarding wizard**.

### Upgrade Notes

- **Cloud LLM API keys must be re-entered.** API keys for cloud LLM providers
  (e.g. OpenAI, Anthropic) that were set via the Settings UI before this release
  must be re-entered by an admin — they are now stored deployment-wide
  (system-scoped) rather than per-user.

### Security

- **Per-user tenant isolation** (migration 0094): all source API keys, LLM
  provider keys, and Zotero credentials are now strictly scoped per user.
  Extractions, entity records, and Zotero sync are stamped and filtered by
  `user_id`. Cross-user visibility of these records is no longer possible.
- **Cloud LLM provider keys are now system-scoped and admin-gated.** Keys for
  cloud providers (OpenAI, Anthropic, etc.) are stored at the deployment level
  and may only be configured by an admin.
- **SMTP-SSRF hardening.** The SMTP host is validated against a blocklist of
  non-public address ranges at both save and send time. An
  `ALLOW_PRIVATE_SMTP_HOST` escape-hatch env var is available for on-premises
  mail servers.
- **Credential/auth hardening.** Telegram `bot_token` is now Fernet-encrypted
  at rest. Advisory locks guard concurrent authentication flows. Admin and setup
  log entries hash email addresses before writing to the log.
- **Source API keys (S2/OpenAlex/PubMed) encrypted at rest.** All per-user
  source credentials are stored encrypted via the existing MultiFernet scheme.
- **PubMed/OpenAlex author-parameter injection hardened.** Author search
  parameters are validated and sanitised before reaching the upstream API.
- **PDF-download SSRF filter blocks CGNAT.** The URL pre-flight check now
  rejects CGNAT and other non-routable ranges in addition to RFC-1918 space.
- **Rate-limiter ignores malformed X-Forwarded-For hops.** Invalid IP tokens in
  the XFF header are silently skipped instead of causing a 500.
- **`/infra-events` rejects oversize request bodies.** A hard body-size limit is
  enforced on the infrastructure-event endpoint.
- **Telegram base-URL scheme validation.** The configured Telegram API base URL
  must use an `http(s)://` scheme; other schemes (e.g. `javascript:`) are
  rejected at config load to prevent XSS / open-redirect via digest links.

### Changed

- **Unified onboarding wizard.** The former two-wizard flow (pre-auth `/first-run` + post-login `/setup`) has been replaced by a single **Onboarding Wizard** gated by the pre-auth `/api/setup/status` endpoint. The wizard spans the auth boundary internally: it walks system check → SMTP → admin account creation & sign-in → cloud LLM keys → first topic → automation schedule → source API keys → Telegram pairing → done. Old `/first-run` and `/setup` deep links redirect to `/`. The admin-create step is skipped when an admin already exists (resuming setup).

- **`/api/setup/status` now returns `setup_completed`.** The pre-auth setup status endpoint (always HTTP 200, no session required) now includes a `setup_completed: bool` field alongside the existing `configured` and hardware fields. The onboarding gate keys on this field.

- **Telegram bot pairing is token-only.** The bot authenticates chats via the `/pair <token>` flow (token from Settings → Integrations → Telegram). The legacy `/start PAIR_<code>` dashboard-code pairing path is removed, and the `TELEGRAM_CHAT_ID` env var is superseded by `/pair` for identity — it no longer authorises a chat on its own and is retained only as an optional override for the outbound message target. The bot no longer writes to the database directly — all product data flows through the service REST API.

### Fixed

- **GDPR purge succeeds in multi-user deployments** (migration 0095):
  `paper_entities` and `pulse_models` now cascade on user delete, so a
  user-deletion request can no longer fail permanently due to a foreign-key
  constraint.
- **Topic facet filters the research feed.** Clicking a topic facet in the
  Research view now correctly narrows the paper feed; previously it highlighted
  but did not filter.
- **GDPR export includes per-user extractions and entities.** The data-export
  package now contains the logged-in user's paper extractions and entity rows.
- **`daily_log` analytics no longer NULL-breaks.** Aggregate queries over the
  daily log table are guarded against NULL entries that previously caused 500
  errors in analytics.
- **Batch-save skips re-analysis of already-processed papers.** Saving a paper
  that already has a completed analysis no longer enqueues a redundant
  `paper.analyze` job.
- **Local-PDF scan attributes papers to the scanning user.** Papers discovered
  via local-PDF scan are now owned by the user who initiated the scan rather
  than being left NULL-owned.
- **Cross-paper RAG retrieves the caller's full library.** The retrieval step in
  multi-paper Q&A now correctly searches the requesting user's entire saved
  library rather than a subset.
- Assorted frontend validation hardening and dead-code cleanup.
- Source-layer `Retry-After` handling and fetch-recording de-duplicated across
  arXiv, Semantic Scholar, OpenAlex, and PubMed source adapters.
- **Models-ready false negative.** The onboarding wizard's system check no longer requires a hardcoded `qwen3:14b` model. "Models ready" now means: the embedder is present (any model matching the configured embedding model prefix, e.g. `qwen3-embedding:*`) AND at least one `qwen3:` chat model is present (`qwen3:4b`, `qwen3:8b`, or `qwen3:14b`). The default install (`setup.sh` → `qwen3:8b` + `qwen3-embedding:4b`) correctly reports ready. The check also distinguishes "still pulling" from "Ollama unreachable".
- **Pomodoro auto-start from stale persisted timer.** A Pomodoro session that was still running when the browser closed no longer auto-starts on the next page load. The timer state is correctly treated as stale across sessions.
- **Cross-user Pomodoro / dismissed-flag state leak.** Timer and dismissed-flag state no longer leaks between users on a shared browser.
- **My Day — calm empty state for the Pulse hero card.** When no Pulse deck exists yet, the My Day Pulse hero card shows a calm "No Pulse for today yet — generate one" call-to-action instead of a red error panel. Red error UI is reserved for genuine backend failures.
- **Mobile responsive fixes.** Projects rail, admin tables, analytics KPI band, mobile facet drawer, TopBar, My Day layout, and the chat surface are now correctly laid out on narrow viewports.
- **Logs preset filters restored on load.** The Logs page preset now re-applies its filter selections when the page is loaded or navigated to.
- **Single-tenant background-job attribution.** Task-completion and paper-summary jobs attribute activity to the owner's account (previously recorded as NULL in single-tenant deployments).

### Migrations

- **0092** — Re-owns legacy NULL-owned product rows (projects, tasks, milestones, daily_log) to the single admin account (single-admin deployments only).
- **0093** — Adds `papers.zotero_citation_key` for Zotero citation-key push.
- **0094** — Per-user scoping of extractions, entity records, Zotero sync, and notes.
- **0095** — Cascades `paper_entities` + `pulse_models` on user delete (GDPR purge correctness).

---

## v0.5.0 (2026-05-24)

### Overview

This release consolidates six weeks of audit and hardening in preparation for the eventual public launch. Approximately 120 findings across security, correctness, architecture, and developer experience were addressed. The core RAG pipeline, spaced-repetition learning system, and daily executive-function interface are now fully hardened for multi-tenant self-hosting. Major work included a cross-tenant audit (all data paths verified user-scoped), a dependency security pass (PDF engine migrated to Docling, closing transitive CVEs), and extensive public-readiness remediation. The migration history was squashed into a single `db/init.sql` baseline with new migrations starting at 0089.

### Detailed changes (2026-05-26)

A six-week audit-and-remediation pass closed roughly 120 findings ahead of the eventual public launch. The themes below capture user-visible and administrator-visible changes; commit-level detail follows in the per-area sections.

**Security.** The background-job Server-Sent Events stream now requires an authenticated session — it previously accepted unauthenticated subscriptions and returned job state for the NULL user. All cross-user data paths were re-audited: project recommendations, paper-source feedback, author alerts, and review-deck queries are now scoped to the logged-in user, and admins cannot read other users' research data. Prompt-injection vectors in PDF body text, paper titles, discovery snippets, and tracked-author bios are stripped before reaching the model with a documented prompt-shape contract enforced by an AST check. Container processes drop privileges, run with `no-new-privileges` set, and ship with a root-level `.dockerignore` so secret files and host-bound paths cannot accidentally land in the build context. Lock-file integrity (Python `uv.lock`, npm `package-lock.json`) is now verified against registry pins at install. Append-only audit logs reject `UPDATE`/`DELETE` at the PostgreSQL rule level, and the pairing-code length, rating regex, and ProjectManager method signatures were tightened against malformed input.

**Correctness.** Several long-standing cross-tenant bugs were fixed: the recommender's project query now filters by `user_id`; author-alert dedupe is per-user instead of global; the Zotero push flow no longer leaks `paper_id` across sessions. A handful of API surfaces that previously returned 200 with inconsistent envelopes now return one shape, and the streaming Chat error path surfaces transport errors to the UI instead of swallowing them. The `paper_sources` table and `PaperSource` abstract base were brought into symmetry so the catalog the UI shows matches what the ingestion job actually runs.

**Architecture.** Two oversized modules were split by responsibility: `entities.py` (814 LOC) became a typed router + a Postgres adapter + a Qdrant adapter; `routers/settings.py` was decomposed by settings domain. A new internal Telegram bot API removes the previous Telegram-bot → paper-ingestion DB-coupling. The migration history was squashed: the 88-file pre-v0.5.0 chain became a single `db/init.sql` bedrock with new migrations starting at `0089`, and `tests/test_baseline_invariants.py` pins the schema invariants.

**Developer experience.** Continuous integration now enforces type-check (Pyright zero errors), a test-shape contract (each test belongs to one of four documented shapes), the LLM prompt-shape AST check, and PII / burned-secret allowlists. The CI workflow was migrated to `astral-sh/setup-uv@v6` with a Python 3.12 pin and `uv sync --frozen`, cutting wall-clock from 8–15 minutes to 4–5 minutes. A pre-commit hook runs the same gates locally.

**Launch groundwork.** This private milestone ships a rewritten README with above-the-fold product screenshots, a Highlights section, and the four-audience deployment path; weekly `dependabot` updates for pip, npm, Docker base images, and GitHub Actions; structured GitHub issue templates (bug report, feature request) with security reports routed to a private GitHub Security Advisory; and a root `SECURITY.md` pointing to the threat model.

### Upgrade Notes

- **Migration baseline squashed.** The 88-file migration chain prior to v0.5.0 was consolidated into `db/init.sql` as the single baseline; new migrations start at 0089. The migration runner detects squashed-init state and applies forward without interruption — administrators upgrading from v0.4.1 or earlier need no manual intervention. See `tests/test_baseline_invariants.py` for the schema invariants pinned.

### Security
- Cross-tenant project leak in recommender: `_refresh_recommendations_for_user` now scopes the projects query to `user_id`.
- Append-only `audit_log` (migration `0090_audit_log_append_only.sql`): blocks `DELETE`/`UPDATE` on the audit table via PG rule.
- Per-user author-alert dedupe (migration `0091_author_alert_log_user_dedupe.sql`): `ON CONFLICT (user_id, tracked_author_id, paper_id)` prevents cross-user alert suppression.
- Owner-override guard tests + audit-log emission.
- Pairing-code length bound, rate-card regex, mandatory `user_id` on 3 ProjectManager methods.

### Bug Fixes
- Email verification flag now respects SMTP exception path.
- Zotero BYTEA decode uses `crypto.resolve_secret_row` (memoryview-safe).
- Summarizer HTTPException propagation guarded at `paper_jobs.py:231, 270`.
- Unread guard in `feed_query.py` resolves contradictory WHERE composition.
- `vector_writer` role boot-time password drop guard.
- Three missing CI smoke secrets.
- `arxiv_source.py` parses `response.content` (bytes) instead of re-encoding `response.text`.
- `weekly_summary.py` ThemeOutput stays as Pydantic instance, no dict.get on LLM output.
- `pulse/job.py` degraded_reason OR-chain preserves earlier value (verified no change needed).
- 13 additional MEDIUM fixes (config validation, GDPR scoping, dynamic model field-name validation, CIDR cache, etc.).
- 9 FRONTEND error-sentinel + per-tab error handling fixes.
- 24 cross-cutting fixes: `decompose_query` doc catalog, sentry-init helper, fixture deduplication, `LockNotAvailableError` simplification, jobs throttle elapsed-seconds, `faux_qdrant` dim-mismatch + null-field guards, email format → replace, `_retry_after_seconds` cap at 3600, `_HAS_QWEN3` guard, `ScoredCandidate` frozen, scheduled `magic_link_tokens` purge, `SourceType.ZOTERO` enum, init-secrets.sh dedupe, profile.sh portable compose ps.

### Hardening
- Enhanced PostgreSQL connection robustness: `_spin_pg_container` adds post-`pg_isready` TCP socket probe (30s deadline + 250ms retries) to eliminate SSL-init race conditions on CI runners.

### Deferred / Documented
- Several architectural and infrastructure items documented in `docs/known-residual-risks.md` with reopen criteria for future releases.



### Documentation
- Published an MkDocs-Material administrator and developer documentation site to GitHub Pages, including a complete end-user guide covering every surface plus plain-English sign-in and account-recovery steps.
- Added a launch-facing `ROADMAP.md` and corrected a batch of documentation drift (migration counts, deprecated environment variables, and stale internal links).
- Documented the `setup.sh --check` pre-flight, single/multi-user modes, and the source HTTP-cache environment variables.



## Early private foundations (v0.1 – v0.4.1)

The v0.1 through v0.4.1 entries summarize the earliest private milestones. The core RAG pipeline was built across this period: multi-source paper discovery (arXiv, Semantic Scholar, OpenAlex, PubMed), PDF extraction with page-level citation provenance, a three-stage LLM-reranked Pulse recommendation engine, and a semantic knowledge graph with entity extraction and contradiction detection. Spaced-repetition learning cards (FSRS) and a daily executive-function interface (My Day, Pomodoro timer, journal, project tracking) were added alongside the recommendation system. Multi-tenancy and security hardening — magic-link authentication, strict user_id scoping across all data paths, per-user FSRS and recommendation state, cross-user isolation CI gates, Docker Secrets, and a container-hardening sweep — were progressively applied from v0.2 onward. The job infrastructure was migrated from a custom worker to procrastinate-backed async task queues with SSE progress streaming. Observability tooling (Langfuse, Sentry, structured audit logging) and a one-shot installer wizard were added in v0.3–v0.4. The v0.4.1 release closed the last known cross-tenant data leaks and completed an independent security and quality review before the v0.5.0 consolidation.
