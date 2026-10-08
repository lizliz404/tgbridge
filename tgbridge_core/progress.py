"""Runner-neutral public progress: durable actions and completed text segments.

Adapters normalize native records here. Private reasoning never becomes a
public event; Telegram delivery and deduplication have one owner, Journal.
"""
import html
import json
import re

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
    method = event.get("method") or event.get("type") or ""
    if method.startswith("item/reasoning/"):
        return {"kind": "activity", "label": "thinking"}
    if method == "turn/plan/updated":
        return {"kind": "activity", "label": "todo", "preview": "Updating plan"}
    if method not in ("item/started", "item/completed", "item.started", "item.completed"):
        return None
    item = (event.get("params") or {}).get("item") or event.get("item") or {}
    kind = item.get("type")
    completed = method in ("item/completed", "item.completed")
    if kind == "reasoning":
        return {"kind": "activity", "label": "thinking"}
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
    if kind == "message_update" and (event.get("assistantMessageEvent") or {}).get("type", "").startswith("thinking_"):
        return {"kind": "activity", "label": "thinking"}
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
    if part.get("type") == "reasoning":
        return {"kind": "activity", "label": "thinking"}
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
    if runner == "opencode" and event.get("type") in ("tool_use", "text", "reasoning"):
        part = dict(event.get("part") or {})
        part.setdefault("type", "tool" if event["type"] == "tool_use" else event["type"])
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


def short_preview(value, secrets=()):
    """Redact before truncating so a cut cannot expose a partial credential."""
    value = " ".join(redact(value, secrets).split())
    return value if len(value) <= 128 else value[:127] + "…"


def activity_body(event, secrets=()):
    """One bounded status preview; full results/diffs never belong in chat."""
    label = short_preview(event.get("label") or "tool", secrets)[:64]
    if label == "thinking":
        heading, preview = "☁️ thinking", ""
    else:
        heading = "🔧 " + label
        if event.get("state") == "failed":
            heading += " · failed"
        inputs = event.get("inputs") or {}
        preview = event.get("preview") or ""
        if isinstance(inputs, dict):
            for key in ("command", "query", "path", "filePath", "input"):
                if inputs.get(key):
                    preview = inputs[key]
                    break
            if not preview and inputs:
                # File-change keys are paths; their values may contain huge diffs.
                preview = ", ".join(str(key) for key in inputs)
        if not isinstance(preview, str):
            preview = json.dumps(preview, ensure_ascii=False)
        preview = short_preview(preview, secrets)
    return html.escape(heading) + ("\n<blockquote>" + html.escape(preview) + "</blockquote>" if preview else "")


class Journal:
    """One delivery ledger for every runner/transport, scoped to one attempt.

    Tool facts stay in a private journal and update one existing status bubble.
    Only completed public text segments create normal chat messages.
    """
    def __init__(self, live, send_text, record_action, secrets=(), update_status=None):
        self.live = live
        self.send_text = send_text
        self.record_action = record_action
        self.secrets = secrets
        self.update_status = update_status or (lambda: None)
        self.actions = {}
        self.text_ids = set()
        self.texts = []

    def emit(self, event):
        if not event:
            return
        if event["kind"] == "activity":
            self.live["activity"] = event
            self.update_status()
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
        entry = self.actions.setdefault(identity, {"event": {}})
        combined = {**entry["event"], **event}
        combined["inputs"] = {**entry["event"].get("inputs", {}), **(event.get("inputs") or {})}
        if combined == entry["event"]:
            return
        entry["event"] = combined
        self.record_action(identity, action_body(combined, self.secrets))
        self.live["activity"] = combined
        self.update_status()

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
