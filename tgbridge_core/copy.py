"""Short English bridge notices. Wording varies; state does not."""
from random import choice

NOTICES = {
    'steering': ('🧭 Steering…', '🧭 Injecting…'),
    'queued': ('⏳ Queued.', '⏳ Up next.'),
}


def notice(state):
    return choice(NOTICES[state])
