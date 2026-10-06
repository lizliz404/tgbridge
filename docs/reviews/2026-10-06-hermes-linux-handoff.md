# Issue #16 — Linux handoff and first source patch

## Source and scope

- Fast-forwarded clean local main from 6566c2e to GitHub main 1016fec.
- Read issue #16 in full; its source is captured in issue-16-source.json.
- Host: liz-dev, Linux. The issue's macOS service evidence is not this host's runtime evidence.
- No commit, push, production restart, provider inference, Telegram acceptance message or remote deployment was performed in this slice.

## Hermes reference actually installed here

Version: hermes-agent 0.19.0, verified from package metadata. No wrapper execution was needed (the user-facing wrapper can install/upgrade software).

Base: /home/liz/.local/share/mise/installs/pipx-hermes-agent/0.19.0/hermes-agent/lib/python3.13/site-packages/

- gateway/stream_events.py (fully read): typed facts separate presentation from runner-owned history; MessageStop marks boundaries; Commentary is a completed public segment. ToolCallFinished does not expose raw output.
- gateway/stream_dispatch.py (fully read): GatewayEventDispatcher routes events through adapter hooks to the delivery sink and tool queue. Presentation must not break the agent loop. This is the architectural reference, not raw tool-dump formatting.
- gateway/systemd_notify.py (fully read): nonblocking sd_notify and event-loop progress/lag supervision, not an unrelated always-on heartbeat thread. TGBridge's synchronous control loop should feed its existing systemd watchdog from actual control-loop progress; no asyncio/platform framework is required.
- gateway/run.py: _load_busy_input_mode near 5321 supports queue/steer/interrupt, default interrupt. TGBridge must adapt to native safe-boundary steering without cancelling active tools, as issue #16 explicitly requires.
- gateway/run.py: _schedule_resume_pending_sessions near 7114 uses durable interruption markers, freshness checks, authorization, restart-loop guard and slot reservation before scheduling. This explains why merely restarting a service or preserving a session ID is not task recovery. TGBridge cannot blindly replay unknown side effects.
- plugins/platforms/telegram/adapter.py near 9045: quote takes precedence over the full replied-to text, with rich-message fallback. Issue #16 calls for separately carrying current message, source reply and selected quote; adapt that stronger contract rather than silently conflating them.

Hermes imports/dependencies/credentials were not copied into TGBridge.

## Live service observations (not deployment acceptance)

At inspection, systemd tgbridge.service was active, MainPID 85413, started 2026-10-05 17:04:19 HKT. Its cgroup contained 85413 and active Pi child 289359. It predates the pulled code; loaded-version evidence was absent.

health.json reported PID 149826 (not alive at the process check), while its recent polling timestamps continued changing. This is inconsistent with systemd ownership. The cause/writer is **not confirmed**, and the snapshot alone cannot be treated as readiness evidence.

The inspecting Pi session was outside the bridge cgroup. Therefore the active bridge child was not assumed to be the current maintenance session. No production stop or second poller was attempted.

## First source patch

- tgbridge_core/runtime.py captures startup revision (when Git exists) and SHA-256 of the entrypoint/core Python files. The snapshot does not follow later disk pulls. Source archives work without .git.
- Startup persists loaded_code in health and records startup PID/identity in the audit trail. Doctor independently compares current source identity to the running snapshot; absent/mismatched evidence fails readiness.
- BridgeStop now inherits BaseException, analogous to process-control exceptions, so generic operation/retry handlers cannot silently turn a shutdown into a recoverable request failure. The main shutdown branch still owns cleanup. The update boundary explicitly propagates it.

This patch is not single-writer locking, a restart transaction, a durable input queue, delivery receipt recovery or automatic task continuation. Those remain open.

## Verification

- Pulled baseline: 89 isolated offline tests passed.
- Patched source: 96 isolated offline tests passed, including the legacy selftest.
- Added regressions cover unknown/stale loaded code, archive identity, optional Git failure, immutable startup identity, stop propagation through network calls and 429/409 backoff.
- Real SIGTERM delivered to a private subprocess blocked inside a mocked network call reaches main's graceful-stop branch and exits zero within the 5-second bound. No production network was used.
- git diff --check passed.

## Unaccepted / next gated checkpoint

- Production new PID + matching loaded_code + complete 50-second polling interval + original-session round trip.
- Resolve health writer ownership, ensure one poller, and implement persistent restart result/requester feedback. Current source checks alone do not close this checkpoint.
- Actual Telegram multi-action/public-segment behavior and safe-boundary injected behavior, not ACK alone.
- Distinct reply/quote/attachment contexts and real Telegram fixtures.
- Durable ingress/queue/execution/delivery boundaries with no blind side-effect replay.
- Isolated SIGKILL/wedge/network recovery; real boot/login and sleep/wake acceptance.

Production restart must be explicitly authorized at an idle/safe task boundary. No deferred restart was scheduled or promised.
