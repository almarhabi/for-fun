"""Streaming from OpenRouter — shared by normal chat, the lead (Kimi) and the agents (GLM).

From the stream it collects: the reply text, the reasoning, the reasoning_details (so they can be
sent back to the model unchanged), tool calls, and usage (when OpenRouter sends it).
"""

import contextlib
import json
import uuid
from dataclasses import dataclass, field


def reasoning_delta(delta) -> tuple[str, list]:
    """OpenRouter sends reasoning both as text (reasoning) and as full details (reasoning_details)."""
    extra = getattr(delta, "model_extra", None) or {}
    details = extra.get("reasoning_details")
    details = details if isinstance(details, list) else []
    text = extra.get("reasoning") or extra.get("reasoning_content") or getattr(delta, "reasoning_content", None)
    if not text and details:
        text = "".join((d.get("text") or d.get("summary") or "") for d in details if isinstance(d, dict))
    return (text if isinstance(text, str) else ""), details


def merge_reasoning_details(acc: list, new: list) -> None:
    """Merges streamed reasoning_details chunks so they can be sent back to the model exactly."""
    for d in new:
        if not isinstance(d, dict):
            continue
        idx = d.get("index")
        target = next((x for x in acc if idx is not None and x.get("index") == idx
                       and x.get("type") == d.get("type")), None)
        if target is None:
            acc.append(dict(d))
            continue
        for k, v in d.items():
            if k in ("text", "summary", "data") and isinstance(v, str):
                target[k] = (target.get(k) or "") + v
            elif v is not None:
                target[k] = v


def split_think_tags(raw: str) -> tuple[str, str]:
    """Some models write their reasoning inside <think>...</think> in the reply itself."""
    s = raw.lstrip()
    if not s.startswith("<think>"):
        return "", raw
    end = s.find("</think>")
    if end == -1:
        return s[7:], ""
    return s[7:end], s[end + 8 :].lstrip()


def usage_dict(usage) -> dict | None:
    """Turns OpenRouter's usage into a simple dict (tokens and cost in USD)."""
    if usage is None:
        return None
    get = (lambda k: usage.get(k)) if isinstance(usage, dict) else \
        (lambda k: getattr(usage, k, None) if getattr(usage, k, None) is not None
         else (getattr(usage, "model_extra", None) or {}).get(k))
    try:
        return {"prompt": int(get("prompt_tokens") or 0), "completion": int(get("completion_tokens") or 0),
                "cost": float(get("cost") or 0.0)}
    except (TypeError, ValueError):
        return None


@dataclass
class Completion:
    content: str = ""
    reasoning: str = ""
    details: list = field(default_factory=list)
    calls: list = field(default_factory=list)   # [{"id","name","args"}]
    usage: dict | None = None
    finish: str | None = None


async def stream_chat(client, messages: list, kw: dict, tool_kwargs: dict | None = None,
                      headers: dict | None = None, on_delta=None) -> Completion:
    """One streamed round with the model. on_delta(raw, reasoning, calls) is called for every chunk."""
    stream = await client.chat.completions.create(messages=messages, stream=True, extra_headers=headers or {},
                                                  **kw, **(tool_kwargs or {}))
    raw, reasoning, details, calls = "", "", [], {}
    usage = finish = None
    try:
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            finish = choice.finish_reason or finish
            delta = choice.delta
            r_text, r_details = reasoning_delta(delta)
            reasoning += r_text
            merge_reasoning_details(details, r_details)
            raw += delta.content or ""
            for tc in delta.tool_calls or []:
                acc = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                acc["id"] = tc.id or acc["id"]
                if tc.function:
                    acc["name"] += tc.function.name or ""
                    acc["args"] += tc.function.arguments or ""
            if on_delta:
                on_delta(raw, reasoning, calls)
    finally:
        with contextlib.suppress(Exception):
            await stream.close()
    _, answer = split_think_tags(raw)
    out = [calls[i] for i in sorted(calls)]
    for c in out:  # some providers send no id — generate one so the tool result can be matched
        c["id"] = c["id"] or "call_" + uuid.uuid4().hex[:12]
    return Completion(answer, reasoning, details, out, usage_dict(usage), finish)


def parse_args(raw: str) -> dict | None:
    """Tool arguments as a dict, or None if the JSON is truncated or invalid."""
    try:
        val = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return val if isinstance(val, dict) else None


def assistant_message(content: str, calls: list, details: list | None = None) -> dict:
    """An assistant message carrying tool calls. Broken arguments become {} so providers don't reject the chat."""
    msg = {"role": "assistant", "content": content,
           "tool_calls": [{"id": c["id"], "type": "function",
                           "function": {"name": c["name"],
                                        "arguments": c["args"] if parse_args(c["args"]) is not None else "{}"}}
                          for c in calls]}
    if details:
        msg["reasoning_details"] = details
    return msg
