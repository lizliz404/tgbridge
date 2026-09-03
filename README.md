# tgbridge

A minimal Telegram bridge for a local OpenCode, Claude, or Codex agent.

A single Python file with **zero dependencies** (stdlib only). No webhooks,
public ports, or databases: Bot API long-poll in, local CLI agent out.

## Features

- **DM + group chats** — groups are mention-triggered (@bot or reply-to-bot), DMs always answer
- **Per-chat sessions** — each chat maps to one native runner session (`/new` forgets the mapping, `/status` inspects it)
- **Optional mid-run steering** — OpenCode's local server transport accepts a new @mention while a run is active, consumes it at the next agent turn, and returns one combined reply; CLI transports keep the existing queued-message behavior
- **Live progress** — one status message, edited in place: elapsed seconds, real-time tool-call trail, and answer tail, read from CLI stdout or the server's persisted transcript
- **Voice notes** — auto-transcribed via any OpenAI-compatible `/audio/transcriptions` API (Groq Whisper, OpenAI, self-hosted) and fed to the agent as text; opt-in via config
- **Scheduled prompts** — `/at 30m <prompt>` (also `s`/`h`); persisted in state and re-armed on restart
- **Agent-initiated outbound** — `python3 tgbridge.py --send <chat_id> <text>` posts to allowlisted chats only, so the agent can proactively notify the group
- **Audit log** — every enqueue/run/send/schedule event appended to `~/.config/tgbridge/audit.jsonl`
- **Never dies silently** — any fatal crash or SIGTERM announces `💀 …` to every allowed chat (best-effort, 3s each) before systemd restarts it; a dead worker thread is detected and respawned; a failed answer delivery retries once, then is audited and saved to `~/.config/tgbridge/undelivered/` instead of vanishing; a missing runner binary warns at startup instead of crashing
- **Ambient group context** — hears recent human conversation but only runs when explicitly @mentioned or replied to
- **Slash-command menu** — `/new`, `/status`, `/at`, `/cancel`, `/help` registered via `setMyCommands`
- **Chat + sender policy** — double allowlist by default; optionally trust all members of specifically allowlisted groups while keeping DMs user-allowlisted
- **Private runtime state** — config, session index, audit log, attachments, and undelivered replies are kept under `~/.config/tgbridge` with private permissions
- **Selftest gate** — `python3 tgbridge.py --selftest` runs at every startup; a bridge that fails its own checks does not go live
- **Emoji lifecycle** — 👀 received → ✅ done / 🔴 error, via `setMessageReaction`
- **Typing indicator** — `sendChatAction` keep-alive for the whole run (re-fired every 4s)
- **Paragraph-aware chunking** — replies split at `\n\n` > `\n` > space (UTF-16 aware, never mid-emoji or mid-code-span), first chunk reply-threaded to your message. Markdown renders as Telegram HTML — code fences with syntax highlighting, tables as bullet groups, merged blockquotes (incl. expandable), bold/italic/strike/spoiler/links — with an automatic clean-plain-text fallback if Telegram ever refuses the HTML
- **Rate-limit friendly** — honors Telegram 429 `retry_after`; poll failures back off exponentially (3s → 30s)

## Setup

**1. Create a bot** with [@BotFather](https://t.me/BotFather) (`/newbot`), copy the token.

**2. Configure** — copy `config.example.json` to `~/.config/tgbridge/config.json`:

```json
{
  "runner": "opencode",
  "runner_mode": "cli",
  "codex_yolo": false,
  "bot_token": "123456:ABC-DEF...",
  "allowed_user_ids": [YOUR_TELEGRAM_USER_ID],
  "allowed_chats": [YOUR_TELEGRAM_USER_ID, -1000000000000],
  "allow_all_users_in_allowed_groups": false,
  "capture_group_context": true,
  "workdir": "/home/you/project",
  "transcribe_base_url": "https://api.groq.com/openai/v1",
  "transcribe_key": "gsk_...",
  "transcribe_model": "whisper-large-v3-turbo"
}
```

Get your user ID from [@userinfobot](https://t.me/userinfobot). Group chat IDs
are negative (`-100...`); get them by posting in the group with the bot in it
and reading the `chat.id` from a getUpdates call.

The three `transcribe_*` keys are optional — omit `transcribe_key` (or the
whole block) and voice notes are answered with a hint instead.

**3. Run** it as a service.

Linux (systemd user service):

```sh
cp systemd/tgbridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tgbridge
journalctl --user -u tgbridge -f
```

macOS (launchd):

```sh
# edit macos/com.liz.tgbridge.plist: user, WorkingDirectory, and runner paths
cp macos/com.liz.tgbridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.liz.tgbridge.plist
tail -f ~/.config/tgbridge/tgbridge.log
```

or just run it inside `tmux`: `OPENCODE_BIN=$(which opencode) python3 tgbridge.py`

**4. Talk to it.** DM the bot, or add it to a group and @mention it.
For groups you may want BotFather → `/setprivacy` → Disable, or make the bot
a group admin, so it can see plain messages.

### Multiple agents in one group

One bridge per machine, one bot per bridge (a bot token allows exactly one
poller). Each collaborator can create their own bot, run their own bridge
against their local agent, and add their bot to the shared group. By default,
put **all** human user IDs in every config's `allowed_user_ids` — the gate checks
the *sender*, so anyone allowlisted can talk to any bot in the group.

If membership of one private, explicitly allowlisted group is the trust boundary,
set `allow_all_users_in_allowed_groups` to `true`. Then any member of that group
may @mention or reply to the bot, while private chats still require an entry in
`allowed_user_ids` and every other group remains blocked. Keep the setting off
for public or loosely controlled groups. Merely discovering the bot username or
numeric bot ID never bypasses the allowed-chat gate.

## Agent-initiated messages

The agent can post to any allowlisted chat without a human prompt, e.g. to
report finished work or ask a question proactively:

```sh
python3 /path/to/tgbridge/tgbridge.py --send -1004347364986 "build done, 3 tests red"
```

The chat must be in `allowed_chats` — the bridge refuses anything else. To make
the tool discoverable, add one line to the machine's `AGENTS.md`:

```markdown
To post to Telegram yourself: python3 /path/to/tgbridge/tgbridge.py --send <chat_id> "<text>"
```

## Configuration

| Key | Meaning |
|---|---|
| `bot_token` | Bot token from BotFather |
| `allowed_user_ids` | Users allowed in DMs and in the default strict group policy |
| `allowed_chats` | Chat IDs the bridge listens in (DM + groups); also gates `--send` |
| `allow_all_users_in_allowed_groups` | If `true`, trust members of allowlisted groups without listing every user ID; DMs remain user-allowlisted (default `false`) |
| `capture_group_context` | Buffer the last 20 eligible human group messages for the next prompt (default `true`) |
| `workdir` | Working directory for the agent |
| `runner` | `opencode` (default), `claude`, or `codex` |
| `codex_yolo` | Pass Codex `--dangerously-bypass-approvals-and-sandbox`; grants authorized Telegram users unsandboxed access as the local OS user (default `false`) |
| `runner_mode` | `cli` (portable default) or `server`; server adapters are capability-based and currently OpenCode implements mid-run steering |
| `server_url` | Local runner server URL (default `http://127.0.0.1:4096`; `OPENCODE_SERVER` remains supported) |
| `server_poll_s` | Local transcript polling interval in server mode (default `0.5`, clamped to `0.1`–`5.0`) |
| `run_timeout_s` | Per-run timeout in seconds (default `900`); partial answers are kept |
| `chunk` | Outgoing reply chunk size (default `3900`, Telegram caps at 4096) |
| `outbox_dir` | Where agents drop files for auto-delivery (default `workdir/.tgbridge-outbox`) |
| `reactions` | `false` disables 👀/👍/👎 emoji lifecycle (default `true`) |
| `model` | Optional model override passed to the runner (`--model` on all three CLIs); empty = runner default |
| `transcribe_base_url` | OpenAI-compatible base URL for transcription (default `https://api.openai.com/v1`) |
| `transcribe_key` | API key; absent = voice notes disabled |
| `transcribe_model` | Whisper model name (default `whisper-1`; Groq: `whisper-large-v3-turbo`) |

Env: `OPENCODE_BIN` overrides the opencode binary path (default: mise shim);
`OPENCODE_SERVER` overrides the default server URL. An explicit `server_url` in
config wins over the environment value. The older `server_runner: true` key is
still accepted as `runner_mode: "server"` for backward compatibility.

Runtime files (never committed) live under `~/.config/tgbridge/` with private
permissions: `state.json` (session index, Telegram offset, scheduled prompts),
`audit.jsonl`, downloaded attachments, and undelivered replies.

### Session storage model

The bridge treats its session map as a disposable index, not as the source of
truth. `state.json` stores `chat_id -> runner session ID`; the runner owns the
actual transcript in its native storage. A group has one shared session, while
each DM or other group gets a different one. `/new` only forgets the mapping so
the next prompt creates a fresh session; it deliberately does not delete the
runner's historical transcript.

## Runners

The bridge drives any of three agent CLIs (config key `runner`):

| runner | non-interactive | resume | notes |
|---|---|---|---|
| `opencode` | `opencode run --format json` | `--session` | default; live tool trail + thinking + cost |
| `claude` | `claude -p --output-format stream-json` | `--resume` | set `CLAUDE_BIN` if not on PATH |
| `codex` | `codex exec --json` | `exec resume <id>` | best-effort; set `CODEX_BIN` |

### Codex permission mode

Codex CLI policy is read-only in some non-interactive environments. The bridge
does not silently override that default. Set `codex_yolo` to `true` only when
the bot token, every allowed chat, and every human authorized in those chats are
trusted to control the host machine. The bridge then adds
`--dangerously-bypass-approvals-and-sandbox` to both new and resumed Codex runs.

This is actual host authority, not merely a more capable chat mode: prompts may
read or modify files, run commands, use locally available credentials, start or
stop processes, and trigger external side effects as the service's OS user.
Discovering the bot ID alone still does not bypass the chat/sender gates, but an
authorized member of a group configured with
`allow_all_users_in_allowed_groups: true` receives this authority.

All runners share the same bridge surface: per-chat sessions, live tool
trail, reactions, chunking. Sessions are titled with a time slug
(`tg 20260903-0958`) on first message.

The default `runner_mode: "cli"` works with all three runners and on both
systemd and launchd installs. True mid-run steering needs a runner transport
that can accept prompts while busy. Today that is OpenCode server mode: start
`opencode serve --hostname 127.0.0.1 --port 4096`, set `runner` to `opencode`
and `runner_mode` to `server`. The bridge polls the server's persisted message
transcript, passes `workdir` on every API call, reports the active mode in
`/status`, and writes steering delivery/failure events to the audit log. An
unsupported runner/mode combination fails explicitly rather than silently
pretending to steer.

By default, human group messages that do not @mention or reply to this bot are
buffered (last 20, with sender, time, and up to 200 characters) and injected
into its next mention-triggered run. The agent can therefore hear the room but
only speaks when addressed. Set `capture_group_context` to `false` to ignore
non-mention traffic.

Telegram itself never delivers messages authored by one bot to another bot,
regardless of admin or privacy mode. Consequently, this ambient context includes
human messages but cannot include another agent bot's progress or final report.
Cross-machine peer-agent report visibility needs a separate shared event channel;
the Telegram group alone cannot provide it. See the
[Telegram Bot FAQ](https://core.telegram.org/bots/faq#why-doesnt-my-bot-see-messages-from-other-bots).

## Design notes

The point of this bridge is to be **the minimal correct core**:
long-poll → gate → session → reply. Absorbed from surveying the field:
streaming progress (claudegram / tg-claude-bot / OpenClaw draft-stream),
voice transcription (claudegram / kerux / tg-claude-bot — cloud-endpoint
variant to stay dependency-free), scheduled prompts and the audit log
(claude-code-telegram), agent-initiated outbound (Telegram-Bridge-MCP idea),
exponential poll backoff (OpenClaw). Deliberately *not* implemented:

- Inline permission buttons (opencode's permission flow lives in its own TUI/config, nothing to relay)
- Webhook/API server, Mini App (new listening sockets and attack surface, never requested)
- MCP stdio server (`--send` covers the use case with a third of the code)
- TTS voice replies (transcription is the valuable direction)
- `/resume` session picker (needs an opencode session-listing surface; `/new` suffices)
- Forum topics as sessions (one session per chat already matches the use case)
- MarkdownV2 rendering (escaping hell; plain text is robust)

Patterns (typing keep-alive, reaction lifecycle, chunk boundaries, 429
handling) were borrowed from studying [hermes-agent](https://github.com/NousResearch/hermes-agent)
and Anthropic's official [claude-plugins-official telegram plugin](https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/telegram).

## Limitations

- One run at a time; cross-chat messages queue, and same-chat messages steer only when the selected server transport supports it
- Bots cannot receive other bots' messages; peer-agent reports need an external shared event channel
- Voice → text only; photos/documents are not ingested
- Per-run timeout via `run_timeout_s` (default 15 minutes)
- No message history — Telegram's Bot API doesn't expose any; sessions are how context persists

## License

MIT
