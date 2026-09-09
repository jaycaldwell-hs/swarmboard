# Research Mode implementation report

Implementation in progress. Each phase is committed only after `make test` passes.

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
  description. New allowlists will add restrictions without relaxing that binding.
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
