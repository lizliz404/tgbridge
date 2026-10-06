# Issue #16: targeted Hermes adaptation + implementation

## Reference version, not an environment upgrade

Installed Hermes package metadata is 0.19.0. PyPI JSON currently reports 0.19.0 as latest (uploaded 2026-07-20). GitHub latest formal release is v2026.9.24 (published 2026-09-24), annotated tag object e3dd27ee2d8b011737a4eea8e3eb3d711ab78690. These are separate distribution/version channels; installed relevant files differ from the formal release. No claim that the GitHub main branch is latest tested code. No runtime/credential/dependency upgrade was performed.

Only relevant formal-release source was fetched under /home/liz/dev/_archived/hermes-reference-v2026.9.24/: gateway/stream_events.py, stream_dispatch.py, systemd_notify.py; reply handling from plugins/platforms/telegram/adapter.py:7014; startup recovery from gateway/run_startup.py:575-623; busy steering from gateway/run_busy.py:559-609; busy-mode config from gateway/run_config_loaders.py:244-247. gateway/run.py imports identified the decomposed modules; it was not read exhaustively.

Mechanisms adapted: typed event facts vs adapter presentation; completed public segment boundaries; plain-language tool chrome instead of raw output as main content; safe-boundary steer with durable queue fallback; pending recovery reserved before scheduling; restored owner authorization; actual control-loop progress, not fake independent heartbeats. TGBridge does not import the Hermes platform or copy its credentials.

## Implemented source changes

- Compact action messages: numbered summary and observed tool status, HTML-escaped expandable details with UTF-16 payload budgeting and credential redaction. Verbose technical view remains configurable. Nonzero Codex command exits are failures even if the native item says completed.
- Separate current instruction, full reply text, selected quote, source IDs/sender, attachment association and unavailable/truncated flags. Source attachments download only after authorization/addressing and non-command handling.
- Fixed real Pi public-segment reconciliation: native transport trims message boundaries while the old Journal did not, causing final text to be resent. Journal now normalizes the same boundaries.
- Immutable startup code identity, doctor deployment mismatch checks, process-ownership lease and PID-owned health writes. Stop is control flow, not a swallowed operation exception.
- Addressed accepted prompts persist in private inputs.json before defer/RPC and offset advancement, with fsync file + parent directory. Failed persistence rolls back in-memory receipts and stops polling before offset commit. Corrupt journal fails closed. Duplicate input identities do not redispatch.
- Queued/unexecuted inputs recover; executing/steering inputs become interrupted, never blindly replayed. /pending, explicit chat-scoped /resume and /result provide continuation/result inspection. Pi ACK is not consumption evidence; consumed inputs track the active task. Failed steering keeps the original durable IDs in the next-turn queue.
- Results are staged before final Telegram sending; unknown send confirmation preserves result state without replaying execution. Complete per-segment crash receipts are still not implemented.
- Automatic failover/fresh-session retry stops after any observed tool action, avoiding replay of unknown side effects. Failover before tool execution is preserved.
- Explicit Linux restart helper tests/restart_local.py persists maintenance/restart-result.json, refuses active children/unsettled inputs/mismatched health ownership, checks new PID + loaded source + full poll interval + session map retention and attempts one bounded requester notice. Failed/ambiguous notices are recorded, not blindly resent.

## Verified evidence

- 125 isolated offline tests pass, including legacy selftest. New coverage includes compact action identity, escaped/long details, reply/quote gating, process ownership, input persistence faults/dedupe/recovery, ACK-vs-consumption, result/send crash boundary, nonreplay after tool execution and restart receipt/feedback failures.
- Real authenticated Pi native RPC smoke tests/smoke_pi_live.py passed twice after the reconciliation fix. It performs read fixture.txt then bash sleep 5; pwd in a private workspace, injects a required marker while bash is active and checks both the native user transcript and changed final answer.
- The final report is issue-16-pi-live-acceptance.json: two distinct action identities, three public segments, steer_in_native_transcript=true, steer_changes_answer=true, final_not_repeated=true. These are real provider/native transport results with Telegram API intercepted, NOT actual Telegram delivery acceptance.
- First real smoke exposed the whitespace-repeat bug, which was fixed and frozen in a regression.
- Real SIGTERM delivered to a private subprocess inside a mocked network call reaches graceful shutdown and exits zero. No production poller was touched.
- git diff --check passes.

## Explicitly not accepted / constraints

Production service remains the old process. Read-only check at 14:29 on liz-dev: systemd MainPID 85413, health PID 149826, loaded_code absent, active service children present. An inconsistent health writer is still unconfirmed in origin. Source changes cannot prove the old runtime has loaded them, and advisory locks only protect newly updated processes sharing this store. No production restart/deployment, commit/push or issue mutation was performed.

The restart helper is a gated operator tool, not an atomic hot-deployment protocol: idle preflight cannot perfectly exclude an inbound message arriving between the final check and stop; legacy in-memory queues lack durable evidence. Do not claim zero-loss legacy deployment. A controlled idle/drain boundary and resolved writer ownership are required for rollout.

Remaining real acceptance: actual Telegram expandable rendering/reply/quote/attachments; startup/end/cross-chat/provider steer races beyond current fixtures; isolated SIGKILL/control-loop wedge/network recovery; real boot/login/sleep/wake; Mac restart adapter; exact per-segment send receipts and durable attachment/schedule transfer. Passive unaddressed burst tails are not yet durable inputs. Queue recovery stores each addressed input independently rather than preserving a precrash merged-burst object. Native session IDs are retained by the existing bridge worker after completion; a crash during a newly created native session still needs earlier session-ID persistence.

This is an implementation slice with native behavioral evidence, not a declaration that issue #16 is closed.
