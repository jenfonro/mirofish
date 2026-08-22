"""OpenAI chat-completions ⇄ Anthropic Messages translation.

Supports text, image parts (data: and http(s) URLs), tool definitions and
tool calls, and true incremental streaming (Anthropic SSE events are mapped to
OpenAI chunks as they arrive instead of buffering the whole response).
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import time
from typing import Any, AsyncIterator, Optional

from .errors import RelayError
from .validate import model_value

_DATA_URL_RE = re.compile(r"^data:(?P<media>[\w.+-]+/[\w.+-]+);base64,(?P<data>.*)$", re.DOTALL)

FINISH_REASONS = {"end_turn": "stop", "stop_sequence": "stop",
                  "max_tokens": "length", "tool_use": "tool_calls",
                  "refusal": "content_filter"}


def _text_of(content: Any) -> str:
    """Flatten OpenAI message content (string or part list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return "" if content is None else str(content)


def _image_block(image_url: Any) -> Optional[dict[str, Any]]:
    url = image_url.get("url") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url:
        return None
    match = _DATA_URL_RE.match(url)
    if match:
        data = "".join(match.group("data").split())
        try:
            base64.b64decode(data, validate=True)
        except Exception:
            return None
        return {"type": "image", "source": {"type": "base64",
                                            "media_type": match.group("media"),
                                            "data": data}}
    if url.startswith("http://") or url.startswith("https://"):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _user_content_blocks(content: Any) -> Any:
    if isinstance(content, str) or content is None:
        return content or ""
    if not isinstance(content, list):
        return str(content)
    blocks: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            blocks.append({"type": "text", "text": str(item)})
            continue
        kind = item.get("type")
        if kind == "image_url" or "image_url" in item:
            block = _image_block(item.get("image_url"))
            if block:
                blocks.append(block)
        elif isinstance(item.get("text"), str):
            blocks.append({"type": "text", "text": item["text"]})
    return blocks or ""


def _tool_use_blocks(tool_calls: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for call in tool_calls if isinstance(tool_calls, list) else []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name", "")).strip()
        if not name:
            continue
        raw_args = function.get("arguments")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else {}
        except json.JSONDecodeError:
            args = {"_raw": str(raw_args)[:2000]}
        blocks.append({"type": "tool_use",
                       "id": str(call.get("id") or ("toolu_" + secrets.token_hex(8))),
                       "name": name,
                       "input": args if isinstance(args, dict) else {"value": args}})
    return blocks


def openai_to_anthropic(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI /v1/chat/completions body into Anthropic Messages shape."""
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            messages.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for raw in payload.get("messages") if isinstance(payload.get("messages"), list) else []:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role", "user"))
        if role in ("system", "developer"):
            text = _text_of(raw.get("content"))
            if text:
                system_parts.append(text)
        elif role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": str(raw.get("tool_call_id", "")),
                "content": _text_of(raw.get("content")),
            })
        elif role == "assistant":
            flush_tool_results()
            blocks: list[dict[str, Any]] = []
            text = _text_of(raw.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            blocks.extend(_tool_use_blocks(raw.get("tool_calls")))
            messages.append({"role": "assistant", "content": blocks if blocks else ""})
        else:
            flush_tool_results()
            messages.append({"role": "user",
                             "content": _user_content_blocks(raw.get("content"))})
    flush_tool_results()

    try:
        max_tokens = int(payload.get("max_tokens")
                         or payload.get("max_completion_tokens") or 4096)
    except (TypeError, ValueError):
        max_tokens = 4096
    out: dict[str, Any] = {
        "model": model_value(str(payload.get("model", ""))),
        "max_tokens": max(1, max_tokens),
        "messages": messages,
    }
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    for key in ("temperature", "top_p"):
        if isinstance(payload.get(key), (int, float)):
            out[key] = payload[key]
    stop = payload.get("stop")
    if isinstance(stop, str) and stop:
        out["stop_sequences"] = [stop]
    elif isinstance(stop, list):
        sequences = [item for item in stop if isinstance(item, str) and item]
        if sequences:
            out["stop_sequences"] = sequences[:4]

    tools = payload.get("tools")
    tool_choice = payload.get("tool_choice")
    if tool_choice == "none":
        tools = None
    if isinstance(tools, list) and tools:
        converted = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = str(function.get("name", "")).strip()
            if not name:
                continue
            converted.append({
                "name": name,
                "description": str(function.get("description", "")),
                "input_schema": function.get("parameters")
                    if isinstance(function.get("parameters"), dict)
                    else {"type": "object", "properties": {}},
            })
        if converted:
            out["tools"] = converted
            if tool_choice == "required":
                out["tool_choice"] = {"type": "any"}
            elif isinstance(tool_choice, dict):
                name = str((tool_choice.get("function") or {}).get("name", "")).strip()
                if name:
                    out["tool_choice"] = {"type": "tool", "name": name}
    return out


def anthropic_to_openai_response(resp: dict[str, Any], model: str) -> dict[str, Any]:
    finish = FINISH_REASONS.get(str(resp.get("stop_reason")), "stop")
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
    blocks = resp.get("content") if isinstance(resp.get("content"), list) else []
    text = "".join(block.get("text", "") for block in blocks
                   if isinstance(block, dict) and block.get("type") == "text")
    tool_calls = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_calls.append({
                "id": str(block.get("id", "")),
                "type": "function",
                "function": {"name": str(block.get("name", "")),
                             "arguments": json.dumps(block.get("input") or {},
                                                     ensure_ascii=False)},
            })
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    return {
        "id": "chatcmpl-" + secrets.token_hex(12),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "total_tokens": prompt + completion},
    }


async def iter_sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Parse an SSE line stream into (event_name, data_json) pairs."""
    event = ""
    data_lines: list[str] = []
    async for line in lines:
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif not line.strip():
            if data_lines:
                raw = "\n".join(data_lines)
                data_lines = []
                if raw == "[DONE]":
                    continue
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    yield (event or str(parsed.get("type", "")), parsed)
            event = ""
    if data_lines:
        try:
            parsed = json.loads("\n".join(data_lines))
            if isinstance(parsed, dict):
                yield (event or str(parsed.get("type", "")), parsed)
        except json.JSONDecodeError:
            pass


class OpenAIStreamTranslator:
    """Map Anthropic streaming events onto OpenAI chat.completion.chunk deltas."""

    def __init__(self, model: str) -> None:
        self.id = "chatcmpl-" + secrets.token_hex(12)
        self.created = int(time.time())
        self.model = model
        self.usage: dict[str, Any] = {}
        self.stop_reason: Optional[str] = None
        self._sent_role = False
        self._tool_index: dict[int, int] = {}  # anthropic block index -> openai tool index

    def _chunk(self, delta: dict[str, Any],
               finish_reason: Optional[str] = None,
               usage: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        chunk: dict[str, Any] = {
            "id": self.id, "object": "chat.completion.chunk",
            "created": self.created, "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            chunk["usage"] = usage
        return chunk

    def feed(self, event: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        kind = str(data.get("type") or event)
        chunks: list[dict[str, Any]] = []
        if kind == "message_start":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            self.usage.update(usage)
            chunks.append(self._chunk({"role": "assistant", "content": ""}))
            self._sent_role = True
        elif kind == "content_block_start":
            block = data.get("content_block") if isinstance(data.get("content_block"), dict) else {}
            if block.get("type") == "tool_use":
                index = int(data.get("index") or 0)
                tool_index = len(self._tool_index)
                self._tool_index[index] = tool_index
                chunks.append(self._chunk({"tool_calls": [{
                    "index": tool_index,
                    "id": str(block.get("id", "")),
                    "type": "function",
                    "function": {"name": str(block.get("name", "")), "arguments": ""},
                }]}))
        elif kind == "content_block_delta":
            delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                if not self._sent_role:
                    chunks.append(self._chunk({"role": "assistant", "content": ""}))
                    self._sent_role = True
                chunks.append(self._chunk({"content": delta["text"]}))
            elif delta.get("type") == "input_json_delta":
                index = int(data.get("index") or 0)
                tool_index = self._tool_index.get(index)
                if tool_index is not None:
                    chunks.append(self._chunk({"tool_calls": [{
                        "index": tool_index,
                        "function": {"arguments": str(delta.get("partial_json", ""))},
                    }]}))
        elif kind == "message_delta":
            delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
            if delta.get("stop_reason"):
                self.stop_reason = str(delta["stop_reason"])
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            self.usage.update(usage)
        elif kind == "message_stop":
            finish = FINISH_REASONS.get(self.stop_reason or "", "stop")
            prompt = int(self.usage.get("input_tokens") or 0)
            completion = int(self.usage.get("output_tokens") or 0)
            chunks.append(self._chunk({}, finish_reason=finish, usage={
                "prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": prompt + completion,
            }))
        elif kind == "error":
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            raise RelayError(str(error.get("message") or "upstream stream error"),
                             502, {"error": error})
        return chunks
