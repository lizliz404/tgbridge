"""Flat bridge-control catalog; slash controls never become agent prompts."""
import re

COMMANDS = (
    ("status", "Status", "/status"),
    ("cancel", "Stop run", "/cancel"),
    ("new", "New session", "/new"),
    ("pending", "Pending tasks", "/pending"),
    ("resume", "Continue task", "/resume <id>"),
    ("result", "Task result", "/result <id>"),
    ("at", "Schedule task", "/at 30m <prompt>"),
    ("runners", "List agents", "/runners"),
    ("runner", "Switch agent", "/runner <name> [model] · /runner default"),
    ("help", "Help", "/help"),
)
COMMAND_NAMES = frozenset('/' + name for name, _, _ in COMMANDS)


def botcmd(text):
    """Parse Telegram command tokens, not Unix paths or arbitrary slash text."""
    parts = text.split(maxsplit=1)
    if not parts or not re.fullmatch(r'/[a-z][a-z0-9_]*(?:@[a-z0-9_]+)?', parts[0], re.I):
        return None, ""
    return parts[0].split('@')[0].lower(), (parts[1] if len(parts) > 1 else '').strip()


def command_menu():
    return [{'command': name, 'description': description}
            for name, description, _ in COMMANDS]


def command_help(bot_username='', group=False):
    body = 'Commands:\n' + '\n'.join(
        f'{usage} — {description}' for _, description, usage in COMMANDS
    )
    body += ('\n\nNote: /new does not stop a run; /cancel does not undo actions.'
             '\nResume interrupted tasks, not old sessions. Runner switches are bot-wide.'
             '\nUse plain text for tasks and skills.')
    if group:
        body += f'\nGroup: /status@{bot_username}, or reply to the bot.'
    return body


def unsupported_command(cmd):
    hint = ' Use /cancel to stop a run.' if cmd in ('/abort', '/stop') else ''
    return f'Unknown command: {cmd}.{hint} See /help. Use plain text for tasks.'
