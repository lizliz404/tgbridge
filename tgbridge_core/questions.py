"""Structured human questions, isolated from steering and tool/status traffic.

Adapted from Hermes clarify's explicit choices + typed answer contract and
Pi's documented select/confirm/input/editor RPC. No answer is preselected.
"""
import secrets
import threading
import time


class Questions:
    def __init__(self, send, edit, acknowledge, record=lambda *a, **k: None):
        self.send = send
        self.edit = edit
        self.acknowledge = acknowledge
        self.record = record
        self.pending = {}
        self.lock = threading.RLock()

    def offer(self, chat, user, title, choices, respond, *, owner, method='select', message='', timeout=None, echo_answer=True):
        choices = list(choices or [])
        if not isinstance(title, str) or not all(isinstance(c, str) and c for c in choices):
            raise ValueError('question title/choices must be text')
        body = '❓ ' + title
        if message:
            body += '\n' + message
        if choices:
            body += '\n\n' + '\n'.join(f'{i + 1}. {choice}' for i, choice in enumerate(choices))
        body += '\n\nChoose or reply.'
        # Reserve room for a 128-character answer receipt, including emoji.
        if len(body.encode('utf-16-le')) // 2 > 3800 or len(choices) > 30:
            raise ValueError('question exceeds Telegram limits; split it before asking')
        key = secrets.token_hex(6)
        rows = [[{'text': choice[:64], 'callback_data': f'q:{key}:{i}'}]
                for i, choice in enumerate(choices)]
        if choices:
            rows.append([{'text': 'Type an answer', 'callback_data': f'q:{key}:text'}])
        rows.append([{'text': 'Cancel', 'callback_data': f'q:{key}:cancel'}])
        entry = {'chat': chat, 'user': user, 'title': title, 'body': body, 'choices': choices,
                 'owner': owner, 'method': method, 'respond': respond, 'message_id': None, 'echo_answer': echo_answer,
                 'deadline': time.monotonic() + timeout / 1000 if timeout else None}
        with self.lock:
            self.pending[key] = entry
        try:
            message_id = self.send(chat, body, rows)
            if message_id is None:
                raise RuntimeError('question delivery unconfirmed')
            with self.lock:
                entry['message_id'] = message_id
            self.record('question_opened', question_id=key, chat_id=chat, owner=owner,
                        method=method, message_id=message_id)
            return key
        except Exception:
            with self.lock:
                self.pending.pop(key, None)
            respond(None)
            raise

    def _live(self, entry, chat, user, message=None):
        if entry['chat'] != chat or (entry['user'] is not None and entry['user'] != user):
            return False
        if message is not None and entry['message_id'] != message:
            return False
        return not entry['deadline'] or time.monotonic() < entry['deadline']

    def has_owner(self, owner):
        with self.lock:
            return any(e['owner'] == owner and (not e['deadline'] or time.monotonic() < e['deadline'])
                       for e in self.pending.values())

    def _finish(self, key, value):
        with self.lock:
            entry = self.pending.pop(key, None)
        if not entry:
            return False
        try:
            entry['respond'](value)
        except Exception:
            self.record('question_response_failed', question_id=key, chat_id=entry['chat'])
            self.edit(entry['chat'], entry['message_id'], '⚠️ Answer delivery unconfirmed.', [])
            return True
        self.record('question_answered', question_id=key, chat_id=entry['chat'],
                    cancelled=value is None)
        result = '\n\nCancelled.' if value is None else '\n\nAnswered.'
        if value is not None and entry['echo_answer']:
            result = '\n\nSelected: ' + (str(value)[:127] + '…' if len(str(value)) > 128 else str(value))
        try:
            self.edit(entry['chat'], entry['message_id'], entry['body'] + result, [])
        except Exception:
            self.record('question_card_edit_failed', question_id=key, chat_id=entry['chat'])
        return True

    def callback(self, query):
        data = query.get('data') or ''
        if not data.startswith('q:'):
            return False
        parts = data.split(':')
        message = query.get('message') or {}
        chat = (message.get('chat') or {}).get('id')
        user = (query.get('from') or {}).get('id')
        with self.lock:
            entry = self.pending.get(parts[1]) if len(parts) == 3 else None
            if not entry or not self._live(entry, chat, user, message.get('message_id')):
                self.acknowledge(query['id'], 'Closed or not yours.')
                return True
            key, selection = parts[1:]
            if selection == 'text':
                self.acknowledge(query['id'], 'Reply with your answer.')
                self.edit(chat, entry['message_id'], entry['body'] + '\n\nReply with your answer.',
                          [[{'text': 'Cancel', 'callback_data': f'q:{key}:cancel'}]])
                return True
            if selection == 'cancel':
                value = None
            elif selection.isdigit() and int(selection) < len(entry['choices']):
                value = entry['choices'][int(selection)]
            else:
                self.acknowledge(query['id'], 'Invalid choice.')
                return True
        self.acknowledge(query['id'], 'Got it.')
        return self._finish(key, value)

    def answer_text(self, chat, user, text, reply_to=None):
        with self.lock:
            matches = [(key, e) for key, e in self.pending.items()
                       if self._live(e, chat, user, reply_to)]
        # Multiple concurrent questions require an explicit reply to one card.
        if len(matches) != 1:
            return False
        key, entry = matches[0]
        value = text.strip()
        if not value:
            return False
        if value.isdigit() and 1 <= int(value) <= len(entry['choices']):
            value = entry['choices'][int(value) - 1]
        if entry['method'] == 'confirm' and value not in ('Yes', 'No'):
            self.edit(chat, entry['message_id'], entry['body'] + '\n\nChoose Yes or No.',
                      [[{'text': choice, 'callback_data': f'q:{key}:{i}'}]
                       for i, choice in enumerate(entry['choices'])])
            return True
        return self._finish(key, value)

    def close_owner(self, owner):
        with self.lock:
            keys = [key for key, e in self.pending.items() if e['owner'] == owner]
        for key in keys:
            with self.lock:
                entry = self.pending.pop(key, None)
            if entry:
                self.record('question_interrupted', question_id=key, chat_id=entry['chat'])
                self.edit(entry['chat'], entry['message_id'], 'Question closed.', [])
