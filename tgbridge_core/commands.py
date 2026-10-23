"""Flat command catalog: one list, two execution owners.

kind "bridge" — answered here, never becomes an agent prompt.
kind "agent"  — forwarded verbatim; the runner executes it (the goal
                commands) and the bridge relays the text it produced.
"""
import re

COMMANDS = (
    ("status", "Status", "/status", "bridge"),
    ("cancel", "Stop run", "/cancel", "bridge"),
    ("new", "New session", "/new", "bridge"),
    ("pending", "Pending tasks", "/pending", "bridge"),
    ("resume", "Continue task", "/resume <id>", "bridge"),
    ("result", "Task result", "/result <id>", "bridge"),
    ("at", "Schedule task", "/at 30m <prompt>", "bridge"),
    ("runners", "List agents", "/runners", "bridge"),
    ("runner", "Switch agent", "/runner <name> [model] · /runner default", "bridge"),
    ("help", "Help", "/help", "bridge"),
    # Goal lifecycle, executed by the runner. The whole goal/sisyphus family is
    # accepted, so an unlisted variant still works when typed.
    ("goal-list", "List goals", "/goal-list", "agent"),
    ("goal-status", "Goal progress", "/goal-status", "agent"),
    ("goal-focus", "Focus a goal", "/goal-focus", "agent"),
    ("goal-resume", "Continue goal", "/goal-resume", "agent"),
    ("goal-pause", "Pause goal", "/goal-pause", "agent"),
    ("goal-clear", "Archive goal", "/goal-clear", "agent"),
    ("goal-direct", "New goal now", "/goal-direct <objective>", "agent"),
)
BRIDGE_COMMANDS = tuple(row for row in COMMANDS if row[3] == "bridge")
AGENT_COMMANDS = tuple(row for row in COMMANDS if row[3] == "agent")
AGENT_COMMAND_FAMILIES = ("goal", "sisyphus")
COMMAND_NAMES = frozenset('/' + row[0] for row in BRIDGE_COMMANDS)


def is_agent_command(cmd):
    """Whether a parsed slash command belongs to a forwarded agent family."""
    name = (cmd or '').lstrip('/')
    return any(name == family or name.startswith(family + '-')
               for family in AGENT_COMMAND_FAMILIES)


def botcmd(text):
    """Parse Telegram command tokens, not Unix paths or arbitrary slash text.

    Dashes belong to a command name: extension commands are conventionally
    `/name-with-dashes`, so the token rule has to admit them.
    """
    parts = text.split(maxsplit=1)
    if not parts or not re.fullmatch(r'/[a-z][a-z0-9_-]*(?:@[a-z0-9_]+)?', parts[0], re.I):
        return None, ""
    return parts[0].split('@')[0].lower(), (parts[1] if len(parts) > 1 else '').strip()


def command_menu():
    return [{'command': name, 'description': description}
            for name, description, _, _ in COMMANDS]


def command_help(bot_username='', group=False):
    body = 'Commands:\n' + '\n'.join(
        f'{usage} — {description}' for _, description, usage, _ in COMMANDS
    )
    body += ('\n\nNote: /new does not stop a run; /cancel does not undo actions.'
             '\nResume interrupted tasks, not old sessions. Runner switches apply only to this chat.'
             '\nGoal commands are run by the agent, not the bridge.'
             '\nUse plain text for tasks and skills.')
    if group:
        body += f'\nGroup: /status@{bot_username}, or reply to the bot.'
    return body


def unsupported_command(cmd):
    hint = ' Use /cancel to stop a run.' if cmd in ('/abort', '/stop') else ''
    return f'Unknown command: {cmd}.{hint} See /help. Use plain text for tasks.'
