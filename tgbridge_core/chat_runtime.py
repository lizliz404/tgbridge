"""One poller, independent chat execution owners, shared durable storage.

Runtime-bound functions are rebound to the chat owner, not to thread-local
state: timer, RPC-reader and steering threads keep the same explicit owner.
"""
from copy import deepcopy
from functools import partial
import os
import queue
import threading
import uuid


class ChatQueue(queue.Queue):
    def __init__(self, start_worker):
        super().__init__()
        self.start_worker = start_worker

    def put(self, item, block=True, timeout=None):
        super().put(item, block, timeout)
        self.start_worker()


class ChatRuntime:
    def __init__(self, parent, chat_id, start_worker, epoch=0):
        self.parent = parent
        self.chat_id = chat_id
        self.execution_id = uuid.uuid4().hex
        self.PENDING_PROMPTS = {}
        self.RETIRED = False
        self.session_state = None
        self.PROMPT_Q = ChatQueue(lambda: start_worker(self))
        self.RUN_LOCK = threading.Lock()
        self.CODEX_WRITE_LOCK = threading.Lock()
        self.PI_RPC_WRITE_LOCK = threading.Lock()
        # Start from the declared schema, never a live peer's mutable state.
        self.RUN_STATE = {
            key: {} if isinstance(value, dict) else [] if isinstance(value, list)
            else False if isinstance(value, bool) else 0 if isinstance(value, int)
            else None
            for key, value in parent.RUN_STATE.items()
        }
        self.SESSION_EPOCH = epoch
        self.STOPPING = False
        self.SERVER_RUNNERS = {
            name: {key: self._rebind(value) if callable(value) else value
                   for key, value in adapter.items()}
            for name, adapter in parent.SERVER_RUNNERS.items()
        }
        self.thread = None

    def _rebind(self, value):
        implementation = getattr(value, '__dict__', {}).get('_runtime_impl')
        return partial(implementation, self) if implementation else value

    def __getattr__(self, name):
        # Resolve dynamically so callers and tests can replace shared services.
        return self._rebind(getattr(self.parent, name))

    def outbox_dir(self, cfg):
        root = os.path.join(self.parent.outbox_dir(cfg), str(self.chat_id))
        return os.path.join(root, self.execution_id) if self.SESSION_EPOCH else root

    def runner_session(self, state, chat_id, runner_name, legacy_runner=None):
        return self.parent.runner_session(
            self.session_state if self.RETIRED else state,
            chat_id, runner_name, legacy_runner=legacy_runner,
        )


class ChatDispatcher:
    def __init__(self, parent, cfg, state):
        self.parent = parent
        self.cfg = cfg
        self.state = state
        self.lock = threading.Lock()
        self.runtimes = {}
        self.retired = []
        self.stopped = False

    def runtime(self, chat_id):
        if chat_id not in self.cfg.get('allowed_chats', []):
            raise ValueError('chat is not allowlisted')
        with self.lock:
            if chat_id not in self.runtimes:
                self.runtimes[chat_id] = ChatRuntime(self.parent, chat_id, self._ensure_worker)
            return self.runtimes[chat_id]

    def new_session(self, chat_id):
        """Detach the old lane; new input never waits for its agent or queue."""
        # Lock order matches ingress: burst ownership before session storage.
        with self.parent.INGRESS_LOCK, self.parent.STATE_LOCK, self.lock:
            old = self.runtimes[chat_id]
            old.session_state = {
                key: {str(chat_id): deepcopy(self.state[key][str(chat_id)])}
                for key in ('sessions', 'session_runners', 'runner_sessions')
                if str(chat_id) in self.state.get(key, {})
            }
            old.RETIRED = True
            new = ChatRuntime(self.parent, chat_id, self._ensure_worker,
                              epoch=old.SESSION_EPOCH + 1)
            self.runtimes[chat_id] = new
            self.parent.clear_runner_sessions(self.state, chat_id)
            # Keep timers/queued inputs on their captured owner. Retired lanes
            # drain and stop without being supervised back into existence.
            # Timers and delayed steer fallbacks may still own this lane.
            # Keep it registered until shutdown, even after its worker exits.
            self.retired.append(old)
            return new

    def owners(self):
        with self.lock:
            return list(self.runtimes.values()) + list(self.retired)

    def close(self):
        """Freeze worker creation before signaling all owned active runs."""
        with self.lock:
            self.stopped = True
            owners = list(self.runtimes.values()) + list(self.retired)
            for runtime in owners:
                runtime.STOPPING = True
        with self.parent.INGRESS_LOCK:
            for runtime in owners:
                for batch in runtime.PENDING_PROMPTS.values():
                    timer = batch.get('timer')
                    if timer:
                        timer.cancel()
        return owners

    def _ensure_worker(self, runtime):
        with self.lock:
            if self.stopped:
                return
            if runtime.thread is None or not runtime.thread.is_alive():
                runtime.thread = threading.Thread(
                    target=runtime.worker, args=(self.cfg, self.state),
                    name=f'tgbridge-chat-{runtime.chat_id}', daemon=True,
                )
                runtime.thread.start()

    def release_retired_worker(self, runtime):
        """Atomically release an idle worker before a late enqueue restarts it."""
        with self.parent.INGRESS_LOCK, self.lock, runtime.PROMPT_Q.mutex:
            if (runtime.RETIRED and not runtime.PENDING_PROMPTS
                    and not runtime.PROMPT_Q.unfinished_tasks):
                runtime.thread = None
                return True
        return False

    def supervise(self):
        for runtime in self.owners():
            if runtime.thread is not None and not runtime.RETIRED:
                self._ensure_worker(runtime)

    def consume(self):
        """Route recovery and scheduled entries; never wait for an agent run."""
        while True:
            entry = self.parent.PROMPT_Q.get()
            try:
                chat_id = entry['chat_id'] if isinstance(entry, dict) else entry[0]
                runtime = self.runtime(chat_id)
                runtime.PROMPT_Q.put(entry)
            except Exception as error:
                self.parent.log(f'chat dispatch error: {error}')
                self.parent.audit('dispatch_error', err=str(error)[:200])
            finally:
                self.parent.PROMPT_Q.task_done()
