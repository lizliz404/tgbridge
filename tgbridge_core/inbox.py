"""Durable accepted input lifecycle, without automatic side-effect replay.

A queued item is safe to restart; an executing or steering item is not. Keep
its original prompt and explicit continuation path until a human chooses.
"""
import copy
import json
import os
import threading
import time

from .storage import save_json


class InboxError(RuntimeError):
    """Stop polling rather than acknowledge an input we failed to persist."""


class Inbox:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        try:
            with open(path) as source:
                self.data = json.load(source)
            if not isinstance(self.data, dict):
                raise ValueError('input journal must be an object')
        except FileNotFoundError:
            self.data = {}
        except (OSError, ValueError) as error:
            raise InboxError('cannot read durable inputs') from error

    def _change(self, mutate):
        # Failed disk writes must not leave a phantom in-memory receipt which
        # would suppress Telegram redelivery at the same update/offset.
        with self.lock:
            previous = copy.deepcopy(self.data)
            try:
                result = mutate()
                save_json(self.path, self.data, durable=True)
                return result
            except (OSError, ValueError) as error:
                self.data = previous
                raise InboxError('cannot persist durable inputs') from error

    def receive(self, identity, chat, message, prompt, user_id=None, chat_type=None):
        def mutate():
            if identity in self.data:
                return False
            self.data[identity] = {'chat_id': chat, 'message_id': message, 'prompt': prompt,
                                   'user_id': user_id, 'chat_type': chat_type,
                                   'status': 'received', 'updated_at': time.time()}
            return True
        return self._change(mutate)

    def transition(self, identities, status):
        if not identities:
            return
        def mutate():
            for identity in identities:
                if identity in self.data:
                    self.data[identity].update(status=status, updated_at=time.time())
        self._change(mutate)

    def stage_result(self, identities, result):
        # Persist output BEFORE calling Telegram; a kill in the send/receipt
        # gap must not turn completed execution into a replayable input.
        def mutate():
            for identity in identities:
                if identity in self.data:
                    self.data[identity].update(status='result_ready', result=result,
                                              updated_at=time.time())
        self._change(mutate)

    def settle(self, identities, status, result, delivery_confirmed):
        def mutate():
            for identity in identities:
                if identity in self.data:
                    self.data[identity].update(status=status if delivery_confirmed else 'result_unconfirmed',
                                              result=result, delivery_confirmed=delivery_confirmed,
                                              updated_at=time.time())
        self._change(mutate)

    def result(self, identity, chat):
        with self.lock:
            entry = self.data.get(identity)
            return entry.get('result') if entry and entry['chat_id'] == chat else None

    def recover(self):
        """Reserve replayable queue entries; unknown execution stays interrupted."""
        def mutate():
            ready, interrupted = [], []
            for identity, entry in self.data.items():
                if entry['status'] in ('received', 'queued'):
                    ready.append({'input_ids': [identity], **entry})
                    entry['status'] = 'queued'
                elif entry['status'] in ('executing', 'steering'):
                    entry['status'] = 'interrupted'
                    interrupted.append({'id': identity, **entry})
                elif entry['status'] == 'interrupted':
                    interrupted.append({'id': identity, **entry})
            return ready, interrupted
        return self._change(mutate)

    def pending(self, chat):
        with self.lock:
            return [{'id': key, **copy.deepcopy(entry)} for key, entry in self.data.items()
                    if entry['chat_id'] == chat and entry['status'] not in ('completed', 'cancelled')]

    def continuation(self, identity, chat):
        def mutate():
            entry = self.data.get(identity)
            if not entry or entry['chat_id'] != chat or entry['status'] != 'interrupted':
                return None
            # Persist reservation before enqueue. A crash in between stays queued
            # and is recovered, while a duplicate /resume cannot replay it twice.
            prompt = (
                '[Explicit human continuation of an interrupted task. First inspect '
                'native session history and actual results. Do NOT blindly repeat '
                'commands or external side effects whose outcome is unknown.]\n'
                + entry['prompt']
            )
            entry.update(status='queued', prompt=prompt, updated_at=time.time())
            return {'input_ids': [identity], **copy.deepcopy(entry)}
        return self._change(mutate)
