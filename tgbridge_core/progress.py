"""Runner-neutral public progress: durable actions and completed text segments.

Adapters normalize native records here. Private reasoning never becomes a
public event; Telegram delivery and deduplication have one owner, Journal.
"""
import html
import json
import re

from .rendering import split_chunks


def redact(text, secrets=()):
    text = str(text or "")
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*[\"']?bearer\s+)\S+", r"\1[redacted]", text)
    text = re.sub(
        r"(?i)([\"']?[\w.-]*(?:api[_-]?key|token|password|secret)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s;,}\]\)]+)", r"\1[redacted]", text,
    )
    text = re.sub(r"\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{12,}|github_pat_[\w]+)\b", "[redacted]", text)
    text = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[redacted]@", text)
    return text


def _output(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("type") not in (None, "text", "input_text", "output_text", "inputText"):
            return ""
        return _output(value.get("content") or value.get("text") or value.get("output") or value.get("message"))
    if isinstance(value, list):
        return "\n".join(_output(x) for x in value if isinstance(x, (str, dict)))
    return ""


def action(action_id, label, inputs=None, state=None, output=None):
    if inputs is not None and not isinstance(inputs, dict):
        inputs = {"input": inputs}
    state = {"inProgress": "running", "in_progress": "running",
             "success": "completed", "done": "completed", "error": "failed"}.get(state, state)
    return {"kind": "action", "id": action_id, "label": label,
            "inputs": inputs or {}, "state": state or "unknown", "output": _output(output)}


def text(text_id, content):
    return {"kind": "text", "id": text_id, "text": content}


def codex_event(event):
    method = event.get("method") or event.get("type")
    if method not in ("item/started", "item/completed", "item.started", "item.completed"):
        return None
    item = (event.get("params") or {}).get("item") or event.get("item") or {}
    kind = item.get("type")
    completed = method in ("item/completed", "item.completed")
    if kind in ("agentMessage", "agent_message"):
        return text(item.get("id"), item.get("text")) if completed else None
    state = item.get("status") or ("completed" if completed else "running")
    if kind in ("commandExecution", "command_execution"):
        inputs = {"command": item.get("command"), "cwd": item.get("cwd")}
        if completed and item.get("exitCode", item.get("exit_code")) is not None:
            inputs["exit code"] = item.get("exitCode", item.get("exit_code"))
            if inputs["exit code"] != 0:
                state = "failed"
        return action(item.get("id"), "bash", inputs, state,
                      item.get("aggregatedOutput", item.get("aggregated_output")))
    if kind in ("fileChange", "file_change"):
        changes = item.get("changes") or []
        inputs = {}
        for change in changes:
            name = change.get("path") or "file"
            inputs[name] = change.get("diff") or change.get("kind") or "changed"
        return action(item.get("id"), "file change", inputs, state)
    if kind in ("mcpToolCall", "dynamicToolCall", "mcp_tool_call"):
        label = "/".join(str(item[k]) for k in ("server", "namespace", "tool") if item.get(k))
        args = item.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"input": args}
        return action(item.get("id"), label or "tool", args, state,
                      item.get("contentItems") or item.get("result") or item.get("error"))
    if kind in ("webSearch", "imageView", "imageGeneration", "collabAgentToolCall"):
        inputs = {k: item[k] for k in ("query", "path", "savedPath", "tool", "receiverThreadIds") if item.get(k)}
        return action(item.get("id"), kind, inputs, state)
    return None


def pi_event(event):
    kind = event.get("type")
    if kind in ("tool_execution_start", "tool_execution_end"):
        return action(event.get("toolCallId"), event.get("toolName") or "tool",
                      event.get("args"),
                      "running" if kind.endswith("start") else ("failed" if event.get("isError") else "completed"),
                      event.get("result"))
    if kind == "message_end":
        message = event.get("message") or {}
        if message.get("role") == "assistant":
            return text(message.get("id"), _output(message.get("content")))
    return None


def opencode_part(part, completed_text=True):
    if part.get("type") == "tool" or part.get("tool"):
        state = part.get("state") or {}
        return action(part.get("id") or part.get("callID"), part.get("tool") or part.get("name") or "tool",
                      state.get("input"), state.get("status"), state.get("output") or state.get("error"))
    if part.get("type") == "text" and completed_text:
        return text(part.get("id"), part.get("text"))
    return None


def cli_event(runner, event):
    if runner == "codex":
        return codex_event(event)
    if runner == "pi":
        return pi_event(event)
    if runner == "opencode" and event.get("type") in ("tool_use", "text"):
        part = dict(event.get("part") or {})
        part.setdefault("type", "tool" if event["type"] == "tool_use" else "text")
        return opencode_part(part)
    return None


def opencode_snapshot(response, baseline, v2=False):
    messages = (response or {}).get("data", []) if isinstance(response, dict) else response or []
    messages = sorted(messages, key=lambda m: ((m if v2 else m.get("info", {})).get("time") or {}).get("created", 0))
    for message in messages:
        info = message if v2 else message.get("info") or {}
        if not info.get("id") or info.get("id") in baseline or info.get("role", info.get("type")) != "assistant":
            continue
        complete = bool((info.get("time") or {}).get("completed") or info.get("error"))
        for index, part in enumerate(message.get("content" if v2 else "parts") or []):
            event = opencode_part(part, completed_text=complete)
            if event:
                event["id"] = event.get("id") or f"{info.get('id')}:{index}"
                yield event


def action_body(event, secrets=()):
    lines = [f"🔧 {event['label']} · {event.get('state') or 'running'}"]
    inputs = event.get("inputs") or {}
    if not isinstance(inputs, dict):
        inputs = {"input": inputs}
    for name, value in inputs.items():
        if value is None:
            continue
        if re.fullmatch(r"(?i)[\w.-]*(?:api[_-]?key|token|password|secret)", str(name)):
            value = "[redacted]"
        elif not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{name}:\n{value}")
    if event.get("output"):
        lines.append("output:\n" + event["output"])
    return redact("\n\n".join(lines), secrets)


def compact_action(event, number, secrets=()):
    """Human-first chrome + Telegram's native expandable details, no LLM guesses.

    Tool names describe observed operations; exit state describes only the tool,
    not project success. Commands/diffs/results stay inspectable and redacted.
    """
    label = event.get('label') or 'tool'
    tool = label.rsplit('/', 1)[-1].lower()
    title = {
        'read': '读取文件', 'edit': '修改文件', 'write': '写入文件',
        'file change': '修改文件', 'bash': '执行命令', 'grep': '搜索内容',
        'find': '查找文件', 'ls': '查看目录', 'codemode': '执行工具脚本',
        'websearch': '搜索网页', 'web_search': '搜索网页',
    }.get(tool, '调用工具：' + label)
    state = event.get('state')
    status = {'running': '进行中', 'completed': '已完成', 'failed': '失败',
              'cancelled': '已取消', 'unknown': '状态待确认'}.get(state, '状态待确认')
    icon = {'completed': '✅', 'failed': '⚠️', 'cancelled': '🛑'}.get(state, '🔧')
    heading = html.escape(redact(f'{icon} 动作 {number} · {title[:100]} · {status}', secrets))
    details = action_body(event, secrets)
    # Escaping can expand < and & sixfold. Budget the actual HTML payload,
    # without splitting entities or surrogate pairs, not the unescaped input.
    pieces, buf, size = [], [], 0
    for char in details:
        escaped = html.escape(char)
        cost = len(escaped.encode('utf-16-le')) // 2
        if size + cost > 2800:
            pieces.append(''.join(buf))
            buf, size = [], 0
        buf.append(escaped)
        size += cost
    if buf:
        pieces.append(''.join(buf))
    return [f'<b>{heading}</b>\n<blockquote expandable>{piece}</blockquote>' for piece in pieces]


class Journal:
    """One delivery ledger for every runner/transport, scoped to one attempt.

    Tool start creates durable messages; completion updates those messages,
    preserving commands and file changes. Long actions split at Telegram's
    UTF-16 limit rather than dropping detail. Text segments use the bridge's
    existing confirmed-delivery/recovery path.
    """
    def __init__(self, live, send_text, send_action, secrets=(), compact=False):
        self.live = live
        self.send_text = send_text
        self.send_action = send_action
        self.secrets = secrets
        self.compact = compact
        self.actions = {}
        self.text_ids = set()
        self.texts = []

    def emit(self, event):
        if not event:
            return
        if event["kind"] == "text":
            # Native transports trim message boundaries when building their
            # final answer. Match that normalization before delivery/reconcile.
            content = (event.get("text") or "").strip()
            identity = event.get("id")
            if not content.strip() or (identity and identity in self.text_ids):
                return
            if identity:
                self.text_ids.add(identity)
            self.texts.append(content)
            if self.live.get("missing_segments") or not self.send_text(content, len(self.texts) == 1):
                self.live.setdefault("missing_segments", []).append(content)
                self.live["streamed"] = False
            else:
                self.live["streamed"] = True
            self.live["preview"] = ""
            return
        identity = event.get("id") or f"anonymous-{len(self.actions)}"
        entry = self.actions.setdefault(identity, {"event": {}, "chunks": []})
        combined = {**entry["event"], **event}
        combined["inputs"] = {**entry["event"].get("inputs", {}), **(event.get("inputs") or {})}
        entry["event"] = combined
        chunks = (compact_action(combined, list(self.actions).index(identity) + 1, self.secrets)
                  if self.compact else split_chunks(action_body(combined, self.secrets), 3900, preserve_whitespace=True))
        for index, chunk in enumerate(chunks):
            if index < len(entry["chunks"]):
                message_id, previous = entry["chunks"][index]
                if chunk == previous or message_id is None:
                    continue  # do not replay an ambiguous failed send
                result = self.send_action(chunk, message_id)
                if result is not None:
                    entry["chunks"][index] = (message_id, chunk)
            else:
                message_id = self.send_action(chunk, None)
                entry["chunks"].append((message_id, chunk))

    def remaining(self, answer):
        base = "\n\n".join(self.texts).strip()
        answer = (answer or "").strip()
        if not base:
            return answer
        extra = ""
        if answer == base:
            pass
        elif answer.startswith(base):
            extra = answer[len(base):].strip()
        elif answer.endswith(base):
            extra = answer[:-len(base)].strip()
        else:
            return answer  # a transport returned content it has not published
        missing = list(self.live.get("missing_segments") or [])
        if extra and extra not in missing:
            missing.append(extra)
        return "\n\n".join(missing).strip() or None
