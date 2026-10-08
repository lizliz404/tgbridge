"""One poller, independent chat execution owners, shared durable storage.

Runtime-bound functions are rebound to the chat owner, not to thread-local
state: timer, RPC-reader and steering threads keep the same explicit owner.
"""
from functools import partial
import os
import queue
import threading


class ChatQueue(queue.Queue):
    def __init__(self, start_worker):
        super().__init__()
        self.start_worker = start_worker

    def put(self, item, block=True, timeout=None):
        super().put(item, block, timeout)
        self.start_worker()


class ChatRuntime:
    def __init__(self, parent, chat_id, start_worker):
        self.parent = parent
        self.chat_id = chat_id
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
        self.SESSION_EPOCH = 0
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
        return os.path.join(self.parent.outbox_dir(cfg), str(self.chat_id))


class ChatDispatcher:
    def __init__(self, parent, cfg, state):
        self.parent = parent
        self.cfg = cfg
        self.state = state
        self.lock = threading.Lock()
        self.runtimes = {}
        self.stopped = False

    def runtime(self, chat_id):
        if chat_id not in self.cfg.get('allowed_chats', []):
            raise ValueError('chat is not allowlisted')
        with self.lock:
            if chat_id not in self.runtimes:
                self.runtimes[chat_id] = ChatRuntime(self.parent, chat_id, self._ensure_worker)
            return self.runtimes[chat_id]

    def owners(self):
        with self.lock:
            return list(self.runtimes.values())

    def close(self):
        """Freeze worker creation before signaling all owned active runs."""
        with self.lock:
            self.stopped = True
            owners = list(self.runtimes.values())
            for runtime in owners:
                runtime.STOPPING = True
        with self.parent.INGRESS_LOCK:
            for batch in self.parent.PENDING_PROMPTS.values():
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

    def supervise(self):
        for runtime in self.owners():
            if runtime.thread is not None:
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
