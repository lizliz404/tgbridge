# Runtime health and durable progress review — 2026-10-06

Reviewed upstream `6566c2e` on the existing macOS checkout and LaunchAgent.

## Findings and changes

- The active service was healthy, but its snapshot still contained a prior
  `crash_error` and `exit_error`. Replaying upstream confirmed that starting a
  new process merged those old fields. Startup now creates a fresh snapshot;
  it preserves session state and the existing audit/history files.
- Upstream `--doctor` returned success for a stopped service when a one-off
  Telegram probe and runner availability checks passed. Diagnostics now also
  require a live service PID, a healthy polling status, and a successful poll
  within 180 seconds. Missing, malformed and future timestamps fail explicitly.
- Diagnostics checked the file-configured runner even after `/runner` changed
  the active runner. They now use the same persisted override resolution as the
  worker, including the effective model and transport.
- Tool details were stripped from the editable status, then erased by its final
  summary. A runner-neutral public progress contract now normalizes actions and
  completed assistant messages from Pi, Codex and OpenCode. Each action creates
  a durable Telegram message; completion edits that action's message. Commands,
  file paths, changes and results remain visible after the run.
- Public commentary was discarded by Codex server mode, and CLI replies could
  collapse into one final block. All CLI adapters, Pi RPC, Codex app-server and
  OpenCode v1/v2 snapshots now publish through the same delivery journal.
  Repeated native IDs do not create duplicate messages; unconfirmed text stays
  queued in order; final delivery sends only missing text or a new footer.
- Actions keep credentials redacted and exclude private reasoning. Long
  commands/diffs split without dropping whitespace or UTF-16 content. Native
  action status variants normalize into the common running/completed vocabulary.

## Validation

- Reproduced the stale snapshot and stopped-service false success using the
  upstream functions with private temporary fixtures and mocked network calls.
- 89 isolated offline tests pass on Python 3.9 and Python 3.14, including the
  existing rendering, Pi RPC, steering, failover, storage and selftest gates.
- Real fixture subprocesses exercise Codex RPC and Pi/OpenCode CLI event pipes;
  the existing Pi RPC fixture and both OpenCode server snapshot formats also
  exercise the shared publication path. These are transport regressions, not
  claims of authenticated inference acceptance for every provider.
- In the authorized Telegram conversation, the shared delivery layer published
  three explicitly labelled acceptance actions using real read-only command
  output and the actual file diff. Telegram returned three distinct message IDs
  (41, 42, 43); these are delivery-layer checks, not provider execution tests.
- Ran the updated doctor with the actual LaunchAgent environment: service PID
  alive, recent successful poll, Codex server adapter available and Telegram
  reachable. This validates diagnostics before the service restart; it does
  not claim that the old running process has loaded the new code.
- Restart must wait for this maintenance reply to finish. The bridge owns the
  current Codex child, so stopping it mid-turn would abort the maintenance run.
  After restart, verify a new PID and a complete long-poll interval.

## Deliberately deferred

- Per-session workers: one global queue still lets a long task hold up another
  chat. This is the most useful next extension, but it changes cancellation,
  steering, outbox ownership and persisted state together. It requires its own
  acceptance slice rather than a small maintenance patch.
- No new runner/provider defaults or auto-replay of partially executed agent
  tasks. Upstream already supplies chunk-local retry and ambiguous-timeout
  safeguards; expanding retries would risk duplicate messages or actions.
