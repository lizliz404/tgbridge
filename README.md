# tgbridge

A minimal Telegram bridge for a local [opencode](https://opencode.ai) agent.

~210 lines of Python, **zero dependencies** (stdlib only). No webhooks, no
public ports, no databases: Bot API long-poll in, `opencode run` out.

## Features

- **DM + group chats** — groups are mention-triggered (@bot or reply-to-bot), DMs always answer
- **Per-chat sessions** — each chat gets its own persistent opencode session (`/new` to reset, `/status` to inspect)
- **Emoji lifecycle** — 👀 received → ✅ done / 🔴 error, via `setMessageReaction`
- **Typing indicator** — `sendChatAction` keep-alive for the whole run (re-fired every 4s)
- **Paragraph-aware chunking** — replies split at `\n\n` > `\n` > space, first chunk reply-threaded to your message
- **Rate-limit friendly** — honors Telegram 429 `retry_after`
- **Slash-command menu** — `/new` and `/status` registered via `setMyCommands` at startup
- **Chat + user allowlist** — double gate; unknown chats/users are dropped silently

## Setup

**1. Create a bot** with [@BotFather](https://t.me/BotFather) (`/newbot`), copy the token.

**2. Configure** — copy `config.example.json` to `~/.config/tgbridge/config.json`:

```json
{
  "bot_token": "123456:ABC-DEF...",
  "allowed_user_ids": [YOUR_TELEGRAM_USER_ID],
  "allowed_chats": [YOUR_TELEGRAM_USER_ID, -1000000000000],
  "workdir": "/home/you/project"
}
```

Get your user ID from [@userinfobot](https://t.me/userinfobot). Group chat IDs
are negative (`-100...`); get them by posting in the group with the bot in it
and reading the `chat.id` from a getUpdates call.

**3. Run** it as a user service:

```sh
cp systemd/tgbridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tgbridge
journalctl --user -u tgbridge -f
```

**4. Talk to it.** DM the bot, or add it to a group and @mention it.
For groups you may want BotFather → `/setprivacy` → Disable, or make the bot
a group admin, so it can see plain messages.

## Configuration

| Key | Meaning |
|---|---|
| `bot_token` | Bot token from BotFather |
| `allowed_user_ids` | Telegram user IDs allowed to talk (groups check the *sender*, not the chat) |
| `allowed_chats` | Chat IDs the bridge listens in (DM + groups) |
| `workdir` | Working directory for `opencode run` |

Env: `OPENCODE_BIN` overrides the opencode binary path (default: mise shim).

## Design notes

The point of this bridge is to be **the minimal correct core**:
long-poll → gate → session → reply. Deliberately *not* implemented, on purpose:

- MarkdownV2 rendering (escaping hell; plain text is robust)
- Streaming edit-in-place (needs flood-budget machinery)
- Pairing flow (single-user allowlist is enough)
- Media ingestion (text-only v1)

Patterns (typing keep-alive, reaction lifecycle, chunk boundaries, 429
handling) were borrowed from studying [hermes-agent](https://github.com/NousResearch/hermes-agent)
and Anthropic's official [claude-plugins-official telegram plugin](https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/telegram).

## Limitations

- One run at a time (messages queue via Telegram's offset while the agent works)
- Text-only (no photos/voice/stickers)
- 15-minute per-run timeout
- No message history — Telegram's Bot API doesn't expose any; sessions are how context persists

## License

MIT
