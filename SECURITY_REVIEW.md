# Credential access review — September 9, 2026

Scope: whether someone with the board's HTTP Basic login can retrieve provider
API keys through the app, including configuration edits, traces, events, exports,
errors, static files and provider requests. This is a focused code review and
test exercise, not a guarantee against every possible attack.

## Findings and fixes

| Finding | Impact | Fix |
| --- | --- | --- |
| Turn details appended raw `provider.response` payloads after redacting the turn | An authenticated reader could see a secret if it had been captured in provider metadata | Redact the complete trace response, including provider records |
| Some retry/lineage responses and error paths lacked the shared redactor; validation errors echoed submitted inputs | Captured secrets could be returned in those fields; rejected submitted credentials could be echoed | Apply response redaction consistently and return validation type/location/message without input or exception context |
| Credential field matching missed provider-prefixed names; free-text matching depended on current environment values | An accidentally saved literal key or recognizable old key could remain visible | Reject/scrub credential aliases and redact recognizable provider-key formats plus credential values inside JSON strings |
| User sampling maps could replace request messages/model or supply tools/plugins | Request integrity and the no-new-tools boundary could be bypassed; no key exfiltration was demonstrated | Allow only generation settings and validate before credential resolution or network/process creation |
| Captured persona source metadata and filesystem failures included server paths | A board login could reveal private directory names in state, traces, events, exports, or errors | Mask captured source directories as `server-managed` on output and use generic filesystem/reload errors; preserve original provenance when public settings are saved again |
| Default API documentation loaded third-party JavaScript, fonts, and a favicon | External scripts ran in the authenticated board's origin | Serve a local API reference at `/docs` and `/redoc`, retain authenticated `/openapi.json`, and send `Referrer-Policy: no-referrer` |

Codex argument validation and malformed provider-usage errors were also tightened.
The fixed OpenRouter HTTPS destination/key binding, disabled redirects, restricted
headers and isolated Codex environment remain in place. Models receive no new
tools. The UI's existing help button now reads **Help**.

## Evidence

- Read-only checks used both hosted collaborator accounts. Each account was checked
  against 27 paths: pages, settings/state, persona metadata, OpenAPI, static assets,
  events, and attempts to reach secrets/database files or traverse static paths.
- Current server credentials were read through the owner's Render API solely for
  in-memory comparison. No values were printed, placed in this report, or sent in
  test inputs. Exact values and common encodings were absent from checked responses.
- No known credential values or recognizable key candidates were found in 255
  file versions from all locally available Git refs. Private/env/database files
  are excluded from Git and container build inputs.
- Authenticated secret-file/traversal requests returned 404. Anonymous attempts
  returned 401. Only the minimal health endpoint is public.
- Planted credentials in temporary test databases exercise traces, errors, retry
  responses, participant views, findings/interventions, SSE, legacy exports, JSONL,
  and ZIP members. Tests preserve the original stored artifacts and immutable events.
- Persona provenance fixtures cover structured and nested JSON snapshots, alternate
  JSON escapes, exports, global/per-session settings roundtrips, and reload failures.
  Source masking never rewrites stored prompts, persona files, or audit events.
- Frontend regressions check escaped post content and same-origin navigation/export
  links. Both API reference pages use local CSS with no scripts or external assets.
- The live board had no sessions during the initial scan, so populated history and
  export surfaces were checked using those isolated fixtures rather than creating
  production conversations or making billable model calls.

The final local gate passed: **578 Python tests and 32 frontend tests**. Page
templates render, and `git diff --check` passes. Deployment is verified against the
exact pushed commit, with a repeat authenticated credential scan and asset checks;
the final live deployment result is reported separately in the delivery message.

## Access boundaries

Board collaborators can see environment-variable **names**, models, personas and
conversation history. Both logins have the same board controls, including the
ability to run billable sessions. The fixes protect credential values; they do not
introduce separate administrator/viewer roles or per-user spending controls.

Raw model captures and database backups remain private server files and may contain
original text before redaction. Render administrators and anyone with server/disk
access are outside the board-login boundary. Redaction covers active configured
values, recognized credential fields and known provider-key formats; an arbitrary
opaque retired secret in unlabelled text cannot be identified reliably. Do not put
credentials into conversations or persona files.
