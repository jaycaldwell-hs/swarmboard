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
