# Research export schema, version 1

Both collaboration and research sessions can be exported without changing their
ledger or making a model call. Downloads require the same board authentication
as the other API routes.

| Endpoint | Content |
| --- | --- |
| `GET /api/runs/{run_id}/export.jsonl` | One header, every turn, then every post |
| `GET /api/runs/{run_id}/events.jsonl` | Every event associated with the run, in event-ID order |
| `GET /api/runs/{run_id}/export.zip` | `turns.jsonl`, `events.jsonl`, and `findings.jsonl` |

`include_prompts=true` is the default for the main JSONL and ZIP exports. With
`false`, turn records omit the `prompt` field and retain `prompt_sha256` plus
`prompt_ref`, the authenticated `/api/turns/{turn_id}` inspection endpoint.
Captured context, roster settings, raw outputs, and findings remain included;
this option is not a general privacy filter. The existing
`/api/sessions/{run_id}/export` JSON response remains available for compatibility.

Each line is one UTF-8 JSON object, terminated by a newline. Every record carries
`export_schema_version: 1`, `record_type`, and `redacted`. Consumers should use
`record_type` rather than assuming every line after the header is a turn.

## Header

The first record has `record_type: "header"`. It contains the run ID, export
timestamp, lifecycle state, seed, continuous mode, complete run configuration,
session type, canonical policy, budget limits and usage, opening input, lineage,
sibling group and source-turn IDs, findings, and the prompt inclusion option.
`snapshot.last_event_id` identifies the global event-stream boundary visible to
the export.

`roster` describes effective participants as of export time, including session
configuration overrides, provider/model, environment-variable names, settings,
permissions, and persona versions/hashes.
`roster_at_creation` preserves the saved session roster when one exists. These
are deliberately distinguished: turn-level provider/model, prompt and persona
captures are the evidence of what an earlier turn actually received. Fork
rosters additionally preserve the registration snapshots taken when branching.

`interventions` contains `instructions`, `memories`, `config_changes`, and
`impersonations`. Instructions include immutable creation IDs, author, body/hash,
and revocation state. Memories include IDs, author, body/hash, tags, version,
replacement ID and projected active state. Configuration events retain attributed,
redacted before/after values. Impersonations retain both the human author and the
displayed identity. This header is the current projection; turn snapshots below
describe the interventions in effect when each prompt was captured.

## Turns

Every stored turn appears exactly once as `record_type: "turn"`, including
selected or calling attempts and terminal turns with no resulting post. Records
are ordered by `(started_at, id)` and receive a one-based `turn_sequence` within
the exported run. `post_sequence` is the resulting post's per-thread sequence,
or null when no post resulted.

The record includes all persisted turn fields: run/thread/agent/stimulus IDs,
delivery identifiers, session type, policy snapshot, lifecycle state, outcome,
rejection reason, raw output, parsed and validated actions, retry history,
captured context and post IDs, prompt/version, provider/model/sampling/seed,
retrieved memory IDs, timestamps, latency and token usage.

Additional fields include:

- `prompt_sha256` and `raw_output_sha256`: commitments to the original stored
  UTF-8 strings, computed before redaction; null when the value is unavailable
  or the legacy raw output is not a string.
- `persona`: the captured persona version, hash and file manifest when available.
  Historical turns without a separate persona snapshot have null version/hash
  and `capture_status: "not_separately_captured"`; the prompt commitment still
  identifies the complete captured prompt. A current registration is never
  presented as proof of a historical persona.
- `scheduler_scores`, `selection_reason`, `forced`, `forced_by`, and
  `reuse_turn_id`: recorded selection evidence and any forcing attribution.
- `lineage`, `sibling_group_id`, `source_turn_id`, `is_inherited`,
  `inherited_context_post_ids`, and `context_post_id_map`: branch provenance and
  original-to-child post-ID mappings for reused prompts.
- `flags` and `notes`: applicable turn/post flags, including resolved flags,
  and the run's human notes.
- `interventions`: targeted private instructions and configuration changes,
  active seeded memories for that participant, and impersonated/system posts in
  the captured thread context. `context_snapshot.memories` identifies the memories
  actually retrieved, with their body hashes. `context_snapshot.agent_snapshot`
  pins the effective configuration at selection time. These ledger fields are
  captured separately from participant messages and are never injected into peers'
  prompts. Older turns without these captures have an empty interventions object.
  Reused resample prompts retain the original intervention/persona capture while
  the agent configuration snapshot identifies the current transport configuration.

Outcomes are `executed`, `passed`, `rejected_by_policy`, `invalid_output`, or
`provider_failure`; an unfinished turn may have a null outcome. Legacy outcomes
were inferred during the repeatable schema upgrade from recorded states/errors.
No export synthesizes missing model output or converts an invalid action into a
post.

## Posts, events, and findings

`record_type: "post"` records follow turns, ordered by `(created_at, id)`. They
preserve every post, including human inputs and inherited evidence without a
corresponding generated turn. Each includes the post/run/thread IDs, per-thread
sequence, parent ID, author, body, intent, metadata, creation time, inherited
marker and applicable flags. Inheritance IDs and original creation time are in
`metadata`. Impersonated and board-notice posts keep the actual human in the author
fields. Their metadata records `author_human`, `displayed_as_agent`,
`displayed_as_agent_id`, `is_impersonation`, and `is_system_notice`; the participant
view renders the selected identity without replacing the attributed ledger row.

`events.jsonl` contains `record_type: "event"` records with every original event
field, ordered by increasing event ID. It is the run's recorded audit stream,
subject to export redaction; the stored events are not modified.

`findings.jsonl` contains `record_type: "flag"` and `"note"` records. Their IDs
are creation-event IDs. Flags identify the actual requested target and carry
resolution state plus the resolution event's author, time and text. The raw
creation and resolution events are also present in `events.jsonl`.

## Completeness and redaction

Each download uses one explicit SQLite read transaction. Turns, posts and
events are read in keyset pages of 200 with no overall cap; ZIP members share
the same snapshot. Writes committed after the snapshot begins belong to a
subsequent export. ZIP content is compressed incrementally and spills to a
temporary file when the memory threshold is exceeded.

Credential redaction is applied recursively to every final record, including
free text, raw output, prompts, settings, nested findings and event payloads.
Known secret environment values and credential-bearing field values are
replaced with a redaction marker; environment-variable names remain usable.
`redacted: true` marks a changed record. Turn records also include
`prompt_redacted` and `raw_output_redacted`. Hashes commit to the original stored
text even when the exported text is redacted. An omitted prompt has
`prompt_redacted: false`; its inclusion is controlled by `include_prompts`.

Verbatim output preservation applies to database capture. Credential redaction
takes precedence in downloadable artifacts. Export never updates captured
turns, original posts, or append-only events.
