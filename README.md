# tgbridge

A minimal Telegram bridge for a local [opencode](https://opencode.ai) agent.

~590 lines of Python, **zero dependencies** (stdlib only). No webhooks, no
public ports, no databases: Bot API long-poll in, `opencode run` out.

## Features

- **DM + group chats** — groups are mention-triggered (@bot or reply-to-bot), DMs always answer
- **Per-chat sessions** — each chat gets its own persistent opencode session (`/new` to reset, `/status` to inspect)
- **Live progress** — one status message, edited in place: elapsed seconds, real-time tool-call trail, tail of the answer as it streams (agent stdout is read live via `Popen`, not buffered)
- **Voice notes** — auto-transcribed via any OpenAI-compatible `/audio/transcriptions` API (Groq Whisper, OpenAI, self-hosted) and fed to the agent as text; opt-in via config
- **Scheduled prompts** — `/at 30m <prompt>` (also `s`/`h`); persisted in state and re-armed on restart
- **Agent-initiated outbound** — `python3 tgbridge.py --send <chat_id> <text>` posts to allowlisted chats only, so the agent can proactively notify the group
- **Audit log** — every enqueue/run/send/schedule event appended to `~/.config/tgbridge/audit.jsonl`
- **Emoji lifecycle** — 👀 received → ✅ done / 🔴 error, via `setMessageReaction`
- **Typing indicator** — `sendChatAction` keep-alive for the whole run (re-fired every 4s)
- **Paragraph-aware chunking** — replies split at `\n\n` > `\n` > space, first chunk reply-threaded to your message
- **Rate-limit friendly** — honors Telegram 429 `retry_after`; poll failures back off exponentially (3s → 30s)
- **Slash-command menu** — `/new`, `/status`, `/at` registered via `setMyCommands`
- **Chat + user allowlist** — double gate; unknown chats/users are dropped silently

## Setup

**1. Create a bot** with [@BotFather](https://t.me/BotFather) (`/newbot`), copy the token.

**2. Configure** — copy `config.example.json` to `~/.config/tgbridge/config.json`:

```json
{
  "bot_token": "123456:ABC-DEF...",
  "allowed_user_ids": [YOUR_TELEGRAM_USER_ID],
  "allowed_chats": [YOUR_TELEGRAM_USER_ID, -1000000000000],
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
# edit macos/com.liz.tgbridge.plist: WorkingDirectory + OPENCODE_BIN paths
cp macos/com.liz.tgbridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.liz.tgbridge.plist
tail -f /tmp/tgbridge.log
```

or just run it inside `tmux`: `OPENCODE_BIN=$(which opencode) python3 tgbridge.py`

**4. Talk to it.** DM the bot, or add it to a group and @mention it.
For groups you may want BotFather → `/setprivacy` → Disable, or make the bot
a group admin, so it can see plain messages.

### Multiple agents in one group

One bridge per machine, one bot per bridge (a bot token allows exactly one
poller). Each collaborator creates their own bot, runs their own bridge against
their own local opencode, and adds their bot to the shared group. Put **all**
human user IDs in every config's `allowed_user_ids` — the gate checks the
*sender*, so anyone allowlisted can talk to any bot in the group.

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
| `allowed_user_ids` | Telegram user IDs allowed to talk (groups check the *sender*, not the chat) |
| `allowed_chats` | Chat IDs the bridge listens in (DM + groups); also gates `--send` |
| `workdir` | Working directory for the agent |
| `runner` | `opencode` (default), `claude`, or `codex` |
| `transcribe_base_url` | OpenAI-compatible base URL for transcription (default `https://api.openai.com/v1`) |
| `transcribe_key` | API key; absent = voice notes disabled |
| `transcribe_model` | Whisper model name (default `whisper-1`; Groq: `whisper-large-v3-turbo`) |

Env: `OPENCODE_BIN` overrides the opencode binary path (default: mise shim).

State files (both machines, never committed): `~/.config/tgbridge/state.json`
(sessions, offset, scheduled prompts) and `audit.jsonl`.

## Runners

The bridge drives any of three agent CLIs (config key `runner`):

| runner | non-interactive | resume | notes |
|---|---|---|---|
| `opencode` | `opencode run --format json` | `--session` | default; live tool trail + thinking + cost |
| `claude` | `claude -p --output-format stream-json` | `--resume` | set `CLAUDE_BIN` if not on PATH |
| `codex` | `codex exec --json` | `exec resume <id>` | best-effort; set `CODEX_BIN` |

All runners share the same bridge surface: per-chat sessions, live tool
trail, reactions, chunking. Sessions are titled with a time slug
(`tg 20260903-0958`) on first message.

Group messages that don't @mention the bot are buffered (last 20, with
sender + time) and injected as passive context into the next
mention-triggered run — so the agent isn't deaf to the conversation,
but only speaks when spoken to.

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

- One run at a time (messages queue via Telegram's offset while the agent works)
- Voice → text only; photos/documents are not ingested
- 15-minute per-run timeout
- No message history — Telegram's Bot API doesn't expose any; sessions are how context persists

## License

MIT
