# Runtime health review — 2026-10-06

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

## Validation

- Reproduced the stale snapshot and stopped-service false success using the
  upstream functions with private temporary fixtures and mocked network calls.
- 78 isolated offline tests pass on Python 3.9 and Python 3.14, including the
  existing rendering, Pi RPC, steering, failover, storage and selftest gates.
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
