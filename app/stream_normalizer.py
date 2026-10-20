import codecs
from copy import deepcopy
from dataclasses import dataclass
import json
import logging
import re
from typing import Any, AsyncGenerator, Dict, List

logger = logging.getLogger("liber2api.normalizer")


@dataclass
class _ChoiceState:
    has_started_content: bool = False
    in_think_tag: bool = False
    think_buffer: str = ""
    template: Any = None


class StreamReasoningNormalizer:
    """Normalize reasoning independently for each choice without replaying deltas."""

    def __init__(self):
        self._states: Dict[Any, _ChoiceState] = {}

    @property
    def has_started_content(self) -> bool:
        # Retain the original choice-zero inspection API.
        return self._states.get(0, _ChoiceState()).has_started_content

    def normalize_json_response(self, resp_body: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize non-streaming chat completion JSON response."""
        choices = resp_body.get("choices", [])
        for ch in choices:
            msg = ch.get("message", {})
            r = msg.get("reasoning")
            rc = msg.get("reasoning_content")
            if r and not rc:
                msg["reasoning_content"] = r
            elif rc and not r:
                msg["reasoning"] = rc
        return resp_body

    @staticmethod
    def _append_text(state, parts, value, reasoning=False):
        if not value:
            return
        if reasoning and not state.has_started_content:
            delta = {"reasoning": value, "reasoning_content": value}
        else:
            state.has_started_content = True
            delta = {"content": value}
        if parts and parts[-1].keys() == delta.keys():
            for key in delta:
                parts[-1][key] += delta[key]
        else:
            parts.append(delta)

    def _content_parts(self, state, value, parts, final=False):
        remaining = state.think_buffer + value
        state.think_buffer = ""
        while remaining:
            marker = "</think>" if state.in_think_tag else "<think>"
            position = remaining.find(marker)
            if position >= 0:
                self._append_text(state, parts, remaining[:position], state.in_think_tag)
                remaining = remaining[position + len(marker):]
                state.in_think_tag = not state.in_think_tag
                continue
            # Keep only a possible tag prefix; stream all unambiguous text now.
            keep = 0
            if not final:
                for size in range(1, min(len(marker), len(remaining) + 1)):
                    if remaining.endswith(marker[:size]):
                        keep = size
            if keep:
                state.think_buffer = remaining[-keep:]
                remaining = remaining[:-keep]
            self._append_text(state, parts, remaining, state.in_think_tag)
            break

    def _process_choice(self, choice, state):
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            pending = []
            if choice.get("finish_reason") is not None:
                self._content_parts(state, "", pending, final=True)
            return [{**({"index": choice["index"]} if "index" in choice else {}), "delta": part}
                    for part in pending] + [deepcopy(choice)]
        parts = []
        reasoning = delta.get("reasoning") or delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            self._append_text(state, parts, reasoning, True)
        content = delta.get("content")
        if isinstance(content, str):
            self._content_parts(state, content, parts)
        if choice.get("finish_reason") is not None:
            self._content_parts(state, "", parts, final=True)
        extras = {k: deepcopy(v) for k, v in delta.items()
                  if k not in ("content", "reasoning", "reasoning_content")}
        if not parts:
            # Preserve empty/null text deltas, but not a buffered tag prefix.
            parts = [{k: deepcopy(v) for k, v in delta.items()
                      if k != "content" or not isinstance(v, str) or not v}]
        parts[0].update(extras)
        output = []
        for number, part in enumerate(parts):
            item = {"delta": part}
            if "index" in choice:
                item["index"] = choice["index"]
            if number == len(parts) - 1:
                # Finish reason/logprobs belong only to the final text fragment.
                item.update({k: deepcopy(v) for k, v in choice.items() if k not in ("delta", "index")})
            output.append(item)
        return output

    def process_chunk_dict(self, chunk: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(chunk, dict):
            return [chunk]
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            if choices == [] and chunk.get("usage") is not None:
                return self.flush() + [deepcopy(chunk)]
            return [deepcopy(chunk)]
        if any(not isinstance(c, dict) or not isinstance(c.get("index", i), int)
               for i, c in enumerate(choices)):
            return [deepcopy(chunk)]
        sequences = []
        envelope = {k: deepcopy(v) for k, v in chunk.items() if k not in ("choices", "usage")}
        for position, choice in enumerate(choices):
            index = choice.get("index", position)
            state = self._states.setdefault(index, _ChoiceState())
            state.template = (deepcopy(envelope), {k: deepcopy(v) for k, v in choice.items() if k == "index"})
            sequences.append(self._process_choice(choice, state))
        frames = []
        for position in range(max(map(len, sequences))):
            frame = deepcopy(envelope)
            frame["choices"] = [seq[position] for seq in sequences if position < len(seq)]
            frames.append(frame)
        if "usage" in chunk:
            frames[-1]["usage"] = deepcopy(chunk["usage"])
        return frames

    def flush(self) -> List[Dict[str, Any]]:
        """Flush incomplete tag prefixes before terminal metadata, DONE, or EOF."""
        frames = []
        for state in self._states.values():
            if not state.think_buffer:
                continue
            parts = []
            self._content_parts(state, "", parts, final=True)
            envelope, choice = state.template
            frame = deepcopy(envelope)
            frame["choices"] = [{**choice, "delta": part} for part in parts]
            frames.append(frame)
        return frames


def _encode_chunk(chunk):
    return ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")


def _normalize_event(lines, normalizer):
    values = []
    other = []
    for line in lines:
        field, separator, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            values.append(value if separator else "")
        else:
            other.append(line)
    original = ("\n".join(lines) + "\n\n").encode("utf-8")
    if not values:
        return [original]
    data = "\n".join(values)
    if data.strip() == "[DONE]":
        return [_encode_chunk(chunk) for chunk in normalizer.flush()] + [original]
    try:
        obj = json.loads(data)
    except (ValueError, TypeError):
        logger.debug("Passthrough malformed SSE event")
        return [original]
    if not isinstance(obj, dict):
        return [original]
    frames = normalizer.process_chunk_dict(obj)
    output = []
    for position, frame in enumerate(frames):
        # Keep event/id/retry/comments grouped with the original data event.
        prefix = ("\n".join(other) + "\n").encode("utf-8") if position == 0 and other else b""
        output.append(prefix + _encode_chunk(frame))
    return output


async def normalize_sse_stream(
    raw_stream: AsyncGenerator[bytes, None]
) -> AsyncGenerator[bytes, None]:
    """Decode incrementally and normalize complete SSE events, not individual lines."""
    normalizer = StreamReasoningNormalizer()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    lines = []
    skip_lf = False
    start = True

    def feed(value, final=False):
        nonlocal buffer, skip_lf, start, lines
        if start and value:
            value = value.removeprefix("\ufeff")
            start = False
        if skip_lf and value:
            if value.startswith("\n"):
                value = value[1:]
            skip_lf = False
        buffer += value
        events = []
        consumed = 0
        for match in re.finditer(r"\r\n|\r|\n", buffer):
            line = buffer[consumed:match.start()]
            consumed = match.end()
            if line:
                lines.append(line)
            elif lines:
                events.append(lines)
                lines = []
            skip_lf = match.group() == "\r" and consumed == len(buffer)
        buffer = buffer[consumed:]
        if final:
            if buffer:
                lines.append(buffer)
                buffer = ""
            if lines:
                events.append(lines)
                lines = []
        return events

    try:
        async for raw_bytes in raw_stream:
            for event in feed(decoder.decode(raw_bytes)):
                for output in _normalize_event(event, normalizer):
                    yield output
        for event in feed(decoder.decode(b"", final=True), final=True):
            for output in _normalize_event(event, normalizer):
                yield output
        for chunk in normalizer.flush():
            yield _encode_chunk(chunk)
    finally:
        close = getattr(raw_stream, "aclose", None)
        if close is not None:
            await close()
