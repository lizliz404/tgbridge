#!/usr/bin/env bash
# tgbridge-hourly-check: read-only drift + bridge health report; gated ff-only checkout sync.
# Usage: hourly-check.sh [check|apply|status]   (default: check)
# Contract: per-host single writer via flock; fetch always runs (read-only, safe
# on dirty trees) but sync is ff-only and skipped when wrong-branch/dirty/
# diverged/ahead/active-write; check mode never changes the tree; apply needs
# the new SHA pinned in the allow-gate; NEVER restarts the bridge (restarts stay
# with tests/restart_local.py --approved); never commit/push/reset/stash/
# switch-branch; never prints secrets.
# Test seams (env overrides; production uses defaults):
#   TGBRIDGE_HOURLY_REPO / _STATE / _GATE / _UNIT
set -euo pipefail

NAME="tgbridge-hourly-check"
REPO="${TGBRIDGE_HOURLY_REPO:-${HOME}/dev/tgbridge}"
BRANCH="main"
UNIT="${TGBRIDGE_HOURLY_UNIT:-tgbridge.service}"
STATE="${TGBRIDGE_HOURLY_STATE:-${HOME}/.local/state/${NAME}}"
GATE="${TGBRIDGE_HOURLY_GATE:-${HOME}/.config/${NAME}/allow}"
MODE="${1:-check}"
TS="$(date '+%Y%m%d-%H%M%S%z')"
EXIT=0
GIT_NOTE="git=n/a"
HEALTH_NOTE="health=n/a"

mkdir -p "$STATE"
: > "$STATE/last-run.log"
LOG="$STATE/last-run.log"
say() { echo "$TS $*" | tee -a "$LOG"; }
record() {
  echo "$TS $*" >> "$STATE/last-result"
  tail -n 200 "$STATE/last-result" > "$STATE/last-result.tmp" && mv "$STATE/last-result.tmp" "$STATE/last-result"
  say "RESULT $*"
}

exec 9>"$STATE/lock"
flock -n 9 || { echo "$TS LOCKED skip (another $NAME holds the lock)"; exit 0; }

if [ "$MODE" = "status" ]; then
  echo "== $NAME status =="
  tail -n 5 "$STATE/last-result" 2>/dev/null || echo "(no runs yet)"
  echo "--- last-success ---"; cat "$STATE/last-success" 2>/dev/null || echo "(none)"
  echo "--- timer ---"
  systemctl --user is-enabled "${NAME}.timer" 2>&1 || true
  systemctl --user is-active "${NAME}.timer" 2>&1 || true
  echo "--- stop:    systemctl --user stop ${NAME}.timer ${NAME}.service"
  echo "--- disable: systemctl --user disable ${NAME}.timer"
  exit 0
fi
if [ "$MODE" != check ] && [ "$MODE" != apply ]; then echo "usage: $0 [check|apply|status]" >&2; exit 2; fi

NEW_SHA=""
if [ ! -d "$REPO/.git" ]; then
  GIT_NOTE="git=SOURCE_ONLY (tar-deployed host, no .git; sync needs approved tar path)"
  say "$GIT_NOTE"
else
  # fetch is read-only and worktree-safe; always attempt, report failure openly
  if ! timeout 60 git -C "$REPO" fetch origin "$BRANCH" >>"$LOG" 2>&1; then
    GIT_NOTE="git=FETCH_FAIL (origin unreachable or timed out; no action, no retry storm)"
    EXIT=1
  else
    HEAD_SHA="$(git -C "$REPO" rev-parse HEAD)"
    ORIGIN_SHA="$(git -C "$REPO" rev-parse "origin/$BRANCH")"
    say "HEAD=$HEAD_SHA ORIGIN=$ORIGIN_SHA"
    if [ "$HEAD_SHA" = "$ORIGIN_SHA" ]; then
      DIRTY_NOW="$(git -C "$REPO" status --porcelain=v1 --untracked-files=no 2>/dev/null | wc -l)"
      echo "$TS $HEAD_SHA" > "$STATE/last-success"
      GIT_NOTE="git=IN_SYNC ($HEAD_SHA)"
      [ "${DIRTY_NOW:-0}" -gt 0 ] && GIT_NOTE="$GIT_NOTE; note: $DIRTY_NOW tracked file(s) dirty vs that SHA (apply would skip until clean)"
    elif git -C "$REPO" merge-base --is-ancestor "$ORIGIN_SHA" HEAD 2>/dev/null; then
      GIT_NOTE="git=SKIP_AHEAD (local HEAD ahead of origin/$BRANCH; human must push/review)"
    elif git -C "$REPO" merge-base --is-ancestor HEAD "$ORIGIN_SHA" 2>/dev/null; then
      NEW_SHA="$ORIGIN_SHA"
      GIT_NOTE="git=BEHIND ($ORIGIN_SHA available)"
    else
      GIT_NOTE="git=SKIP_DIVERGED (branches diverged; human must reconcile)"
    fi
  fi
  # sync eligibility (check mode reports; apply enforces)
  BLOCK=""
  CUR="$(git -C "$REPO" branch --show-current 2>/dev/null || echo ?)"
  [ "$CUR" = "$BRANCH" ] || BLOCK="branch=$CUR"
  if [ -n "$(git -C "$REPO" status --porcelain=v1 --untracked-files=no 2>/dev/null)" ]; then
    say "tracked dirty files (first 5):"
    git -C "$REPO" status --porcelain=v1 --untracked-files=no 2>/dev/null | head -5 | tee -a "$LOG" || true
    BLOCK="${BLOCK:+$BLOCK,}dirty"
  fi
  # active-write guard: tracked files modified in the last 30 min (a worker may be
  # mid-edit but not yet showing dirty vs HEAD, or build outputs churn untracked).
  # python (not find+xargs: appended paths must not follow the expression).
  N_ACTIVE="$(python3 -c "import os,subprocess,sys,time
repo=sys.argv[1]; cut=time.time()-1800
blobs=subprocess.run(['git','-C',repo,'ls-files','-z'],capture_output=True).stdout.split(b'\x00')
n=0
for rel in blobs:
    if not rel: continue
    try:
        if os.path.getmtime(os.path.join(repo,rel.decode('utf-8','surrogateescape')))>cut: n+=1
    except OSError: pass
print(n)" "$REPO" 2>/dev/null || echo 0)"
  [ "$N_ACTIVE" -gt 0 ] && BLOCK="${BLOCK:+$BLOCK,}active-writes($N_ACTIVE)"
  N_UNTRACKED="$(git -C "$REPO" status --porcelain=v1 --untracked-files=normal 2>/dev/null | grep -c '^??' || true)"
  [ "$N_UNTRACKED" -gt 0 ] && say "info: $N_UNTRACKED untracked file(s) present (not blocking)"
  if [ -n "$NEW_SHA" ]; then
    if [ -n "$BLOCK" ]; then
      GIT_NOTE="$GIT_NOTE; SKIP_SYNC ($BLOCK; no reset/stash/force)"
    elif [ "$MODE" = apply ]; then
      ALLOW="$(tr -d '[:space:]' < "$GATE" 2>/dev/null || true)"
      if [ "$ALLOW" != "$NEW_SHA" ]; then
        GIT_NOTE="$GIT_NOTE; PENDING_GATE (write $NEW_SHA into $GATE to approve; no sync yet)"
      elif [ "$(git -C "$REPO" branch --show-current)" != "$BRANCH" ] || \
           [ -n "$(git -C "$REPO" status --porcelain=v1 --untracked-files=no)" ]; then
        GIT_NOTE="$GIT_NOTE; SKIP_SYNC (tree changed during run; no sync)"
      elif git -C "$REPO" merge --ff-only "origin/$BRANCH" >>"$LOG" 2>&1; then
        echo "$TS $NEW_SHA" > "$STATE/last-success"
        GIT_NOTE="$GIT_NOTE; APPLY_OK (ff-only synced to $NEW_SHA; service NOT restarted)"
      else
        GIT_NOTE="$GIT_NOTE; APPLY_FAIL (ff-only merge failed; left for human)"
        EXIT=1
      fi
    else
      GIT_NOTE="$GIT_NOTE; check-mode took no action (apply needs MODE=apply + gate pin)"
    fi
  fi
fi
say "$GIT_NOTE"

# ---- health: actual process version, not just git HEAD ----
ACTIVE="$(systemctl --user show "$UNIT" -p ActiveState --value 2>/dev/null || echo unknown)"
MAINPID="$(systemctl --user show "$UNIT" -p MainPID --value 2>/dev/null || echo 0)"
HJSON="${HOME}/.config/tgbridge/health.json"
if [ -f "$HJSON" ]; then
  HEVAL="$(python3 - "$HJSON" <<'PY' 2>/dev/null || echo HEVAL_READ_FAIL
import json,sys
h=json.load(open(sys.argv[1])); lc=h.get('loaded_code') or {}
print("status=%s pid=%s rev=%s src=%s fails=%s lastok=%s bot=%s" % (
  h.get('status'),h.get('pid'),lc.get('revision'),
  (lc.get('source_sha256') or '')[:12],h.get('consecutive_poll_failures'),
  h.get('last_poll_ok_at'),h.get('bot_username')))
PY
)"
else HEVAL="no health.json"; fi
CG="$(systemctl --user show "$UNIT" -p ControlGroup --value 2>/dev/null || true)"
NPROC=0
if [ -n "$CG" ] && [ -f "/sys/fs/cgroup/$CG/cgroup.procs" ]; then NPROC="$(wc -l < "/sys/fs/cgroup/$CG/cgroup.procs")"; fi
# unsettled set mirrors tgbridge_core/maintenance.py restart guard exactly
UNSETTLED="$(python3 -c "import json,os;d=json.load(open(os.path.expanduser('~/.config/tgbridge/inputs.json')));print(sum(1 for v in d.values() if isinstance(v,dict) and v.get('status') in ('received','queued','executing','steering')))" 2>/dev/null || echo ?)"
UNCONF="$(python3 -c "import json,os;d=json.load(open(os.path.expanduser('~/.config/tgbridge/inputs.json')));print(sum(1 for v in d.values() if isinstance(v,dict) and v.get('status')=='result_unconfirmed'))" 2>/dev/null || echo ?)"
HEALTH_NOTE="health=(active=$ACTIVE mainpid=$MAINPID cgroup_procs=$NPROC unsettled=$UNSETTLED unconfirmed=$UNCONF $HEVAL)"
say "$HEALTH_NOTE"
[ "${NPROC:-0}" -gt 1 ] 2>/dev/null && say "BUSY: agent child present; any restart must wait for idle + approved helper"
[ "${UNSETTLED:-0}" != "0" ] 2>/dev/null && [ "$UNSETTLED" != "?" ] && say "UNSETTLED inputs present; restart helper would refuse"

record "$GIT_NOTE | $HEALTH_NOTE"
exit "$EXIT"
