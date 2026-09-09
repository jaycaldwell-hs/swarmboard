# Research Mode implementation report

All four phases are implemented and passed their sequential `make test` gates.
Final validation: **502 Python tests and 29 frontend tests passed**, with no skips
or expected failures. Each phase has its own commit. Vertex and Ollama adapters
are removed; the supported paths are OpenRouter for open-model peers and
Codex/Astra for Ada, following the user's later instruction.

The immutable ledger, terminal fences and existing action schema remain intact.
Provider calls, local persona files and production data were not used for testing.
Browser visual QA is the remaining verification limitation: the installed browser
runtime cannot initialize. Deterministic API, engine, storage, export and frontend
interaction tests passed. Decisions and compatibility deviations are recorded below.

## Decisions and invariant precedence

- The capability table and detailed session-type rules govern collaboration access:
  force, findings, export, private instructions, hot-swap, memories and participant
  views are available in both types. Policy changes and impersonation are research-only.
  Fork/resample APIs accept collaboration sources but their UI controls appear only
  in research sessions, as specified by the detailed UI section.
- Existing collaboration sessions already relax several conversation policies.
  `production` preserves that existing behavior for collaboration. Research sessions
  interpret the production knobs literally. This resolves the conflict between
  “all protections on” and preserving the existing collaboration tests/behavior.
- The existing hosted provider destination/key binding is stricter than the prompt's
  description. The new allowlists add restrictions without relaxing that binding.
- No live provider calls or changes to private persona files or the local board
  database are needed for verification.

## Phase 1 — Policy knobs, raw capture, and session types (item 3)

- Added `run_policy.py`, canonical production/permissive/custom policy objects,
  creation-time immutable session types, and validation for every run/session/Ada
  creation route. Research policy controls appear only after opting in. Existing
  collaboration defaults and autonomous scheduling behavior remain unchanged.
- Added Turn `session_type`, `policy_snapshot`, `outcome`, `rejection_reason` through
  the repeatable database upgrade. Historical metadata is backfilled without event
  updates; turns capture their policy before calling a provider. Existing event
  names and trigger definitions are unchanged.
- Applied knobs in scheduler eligibility, action validation and fresh commit fences,
  cooldown views, free-thread and cadence dormancy. Research cooldowns use actual
  generated contributions in that run, not global participation in another run.
- Raw-output audit: HTTP gateways already preserved invalid-output and rejection
  text. Codex normalized CRLF when reading its output file; it now decodes bytes.
  The soon-removed Vertex adapter stripped whitespace; removal supersedes that fix.
  Parser errors now retain their source text even outside a provider adapter.
  Capture mode stores unknown JSON fields and invalid raw output without retries,
  action fabrication or a visible post. Strict validation/action schema is unchanged.
- User steering during Phase 1 narrowed providers to open models via OpenRouter and
  Ada via Codex/Astra. Removed Ollama and Vertex adapters, Google SDK dependency,
  unsupported UI/status routes and `vertex.md`. OpenRouter URL/key binding now
  applies locally too. Startup disables unsupported registrations with
  `agent.provider_disabled`, retaining all history. Existing provider tests were
  adapted to OpenRouter equivalents and rejection tests; this is an explicit user
  override of the prompt's requirement that old provider tests remain unchanged.
- Added `SWARMBOARD_LOCAL_OPERATOR` for local attribution. Updated README,
  architecture, session guide, Render provider description and `.env.example`.
- New tests cover repeatable upgrade/unchanged events and triggers, type validation
  without partial writes, policy snapshots and outcomes, permissive loops/duplicates/
  cooldowns, production fences, capture artifacts, strict retries, rerun policy,
  CRLF preservation and conditional research UI. Final gate result recorded below.
- Compatibility decision: the old harness CLI posted its opening as `SYSTEM`
  through the public human API. That conflicts with the new collaboration author
  boundary. The CLI now uses the attributed human identity; its existing test's
  single opening-author expectation changes from SYSTEM to human. Its exchange,
  captured persona and all other assertions remain unchanged.

Phase 1 gate: `make test` passed — 385 Python tests and 11 Node tests.

## Phase 2 — Fork, force next speaker and resample (item 2)

- Added transactional `research.py` and `/api/threads/{id}/fork`,
  `/api/runs/{id}/force-turn`, `/api/turns/{id}/resample`,
  `/api/threads/{id}/forks` and `/api/threads/{id}/participant-view`.
- Forks create Run+Thread+Experiment with inherited post metadata, remapped parent
  IDs, full ancestor lineage, current roster and fresh/remaining budget choices.
  Source rows/events are untouched, including by authenticated request auditing.
  Creation is idempotent; events follow ordinary SSE publication patterns.
- Forced stimuli bypass selection, record author/override, retain commit fences,
  defer production cooldowns and never hand a pass to a different participant.
  Resamples are grouped sibling forks with optional byte-identical prompt reuse.
  Original raw/parsed replies retain their IDs; execution maps inherited parents.
- UI adds Participant tools to both types; research-only secondary fork/resample
  controls, inherited badges, lineage/sibling navigation, nested/collapsed branches
  and a hide-research filter. Extended README, architecture and session guide.
- Decisions: source safety-block forks remain prohibited by the existing no-retry
  invariant. Remaining budgets mean the source's current usage, not reconstructed
  usage at post N. Reused prompts still use current provider configuration; no
  deterministic remote-sampling guarantee is made. Historical participant previews
  combine the selected history prefix with current configuration; exact historical
  configurations remain available through captured turn prompts. Cross-thread
  parent references outside the fork's inherited history are rejected, not invented.
- Tests cover source immutability (including authenticated HTTP/SSE), inherited
  prefixes and fresh quotas, terminal/live/nested forks, remaining budgets,
  forced selection/cooldown/attribution/fencing, resample prompt identity/parent
  mapping, historical participant visibility and conditional frontend controls.

Phase 2 gate: `make test` passed — 424 Python tests and 16 Node tests.

## Phase 3 — Findings capture and export (item 8)

- Added event-only findings services and APIs: post/turn flags, append-only flag
  resolutions, run notes, run findings projection and suggested-tag settings.
  Events are `research.flag_created`, `research.flag_resolved` and
  `research.note_created`. Authored text/tags and original events are immutable;
  terminal history can be annotated without changing conversation content.
- Added complete keyset-paged JSONL transcript/event exports and a ZIP bundle
  with findings. Exports use a consistent database read snapshot, versioned header,
  turns and posts, prompt/raw hashes, lineage, inherited markers, scheduler trace,
  usage and findings. `include_prompts=false` retains hashes and turn references.
  Existing JSON session export remains compatible.
- Added recursive credential redaction across all exported fields and public
  serializers, including secrets embedded in raw output, prompts and annotations.
  Verbatim storage takes precedence internally; credential redaction takes
  precedence in downloads, with original hashes and redaction marking.
  Provider failures now retain returned refusal text/usage when available.
- UI adds findings/notes/resolution controls and flagged-only Activity filtering
  to both session types, plus transcript/events/ZIP links and prompt inclusion.
  Updated README, architecture, SESSIONS, `.env.example`, and new EXPORT.md.
- Tests cover immutable resolution, author identity/idempotency, terminal/both-type
  annotations, free tags, full multi-page exports, failures and invalid artifacts,
  fork/resample lineage, and planted credential values across download surfaces.
- Visual QA limitation: isolated temporary test server starts, but the installed
  browser runtime cannot initialize (`node:process` import is prohibited by its
  execution tool). No browser controls were used after that failure; API and
  frontend DOM/interaction contract tests remain available. No production data
  or live models were used.

Phase 3 gate: `make test` passed — 442 Python tests and 21 Node tests.

## Phase 4 — Human levers (item 4)

- Added `interventions.py`, `interventions_api.py`, `context_views.py`, and shared
  frontend intervention controls. New API paths cover research posts, targeted
  instruction creation/revocation, session participant configuration, memory
  creation/versioning/deactivation, and the read-only intervention projection.
  README, SESSIONS, architecture, EXPORT and `.env.example` document the controls.
- All intervention writes use attributed operator identities, transactional
  repository primitives and operator-scoped retry keys. New events are
  `instruction.created`, `instruction.revoked`, `agent.config_changed`,
  `memory.seeded`, `memory.deactivated` and `research.intervention`. Research posts
  use the unchanged atomic `post.created` path and truthful human author fields.
- Participant contexts and reply routing render the displayed agent or board author.
  Ledger post/event metadata retains actual human and displayed identity. Impersonated
  posts affect neither the participant's generated-turn quota nor cooldown. Both
  generated and ordinary human replies target the perceived author correctly.
- Private instructions append after the target's persona and schema instructions,
  including Ada. Memory IDs/body hashes and active-state projections reach only the
  target's later context. Deactivation and replacement add events without editing
  old memories. Historical previews respect instruction/memory event cutoffs.
- Session overrides capture provider, model, sampling and persona for the next
  selected turn; an already selected turn uses its pinned configuration. Global
  edits emit configuration events for affected sessions. All participants receive
  persona versions/hashes. Ada retains her captured instruction file while a session
  persona edit replaces captured memory content; local persona files are untouched.
- Added `agents.persona_version` through the repeatable database upgrade. A full
  gate caught that adding this metadata to settings changed the existing hosted
  restart contract. Dedicated storage preserves settings exactly; the original
  restart test remains unchanged. No event-table or trigger changes were made.
- Export headers now show effective roster and complete intervention projections;
  each turn retains the intervention/persona evidence associated with its captured
  prompt. Truthful metadata is attached after provider messages are encoded, so it
  never reveals impersonation or another agent's private content in participant
  prompts. Exact-prompt resamples retain original context evidence and capture their
  current transport configuration separately.
- Added restrictive host/key allowlist env settings and recursive validation,
  scrubbing and redaction of nested credentials. The later user request to keep
  only OpenRouter peers and Ada/Astra supersedes the prompt's Ollama/localhost
  defaults. Neither explicit loopback entries nor custom credential names can
  widen the supported OpenRouter pair. Codex credentials remain server controlled.
- UI exposes allowed levers in both Session surfaces, research-only author modes,
  attribution badges and terminal read-only state. Final review fixed resample
  grouping so agent-created follow-up threads remain visible and sample counts
  count runs. Participant preview shows complete system/user messages, including
  targeted instructions and memories.
- Tests cover both session types, immutable attribution/idempotency, terminal
  writes, target-only instructions/Ada/revocation, memory versions/deactivation and
  hashes, historic projections, in-flight and selection-to-dispatch config pinning,
  effective fork rosters, truthful exports, recursive secrets, allowlist rejection
  and startup audits. Deployment remains one service/worker; Dockerfile and
  render.yaml retain their build/test and launch paths. No deployment or live model
  calls were made. Browser visual QA remains unavailable for the reason in Phase 3;
  UI navigation retains existing state-snapshot limits, while exports remain complete.

Phase 4 gate: `make test` passed — 502 Python tests and 29 Node tests.
`git diff --check` passed. The isolated temporary QA server has been stopped.
