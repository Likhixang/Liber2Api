import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

logger = logging.getLogger("liber2api.normalizer")


class StreamReasoningNormalizer:
    """
    Normalizes streaming SSE chunks from vLLM/LibertAI:
    1. Dual-writes reasoning fields: populates both 'reasoning' and 'reasoning_content'.
    2. Splits mixed chunks (containing both reasoning and content) into two separate consecutive SSE frames.
    3. Monotonic State Guard: Once content streaming starts, prevents backwards jumping to thinking mode.
    4. Guards against leading empty content strings causing premature transition out of thinking mode.
    """

    def __init__(self):
        self.has_started_content: bool = False

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

    def process_chunk_dict(self, chunk: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Takes a single parsed chunk dict and returns 1 or 2 normalized chunk dicts.
        """
        choices = chunk.get("choices", [])
        if not choices:
            return [chunk]

        delta = choices[0].get("delta", {})
        r = delta.get("reasoning") or delta.get("reasoning_content")
        c = delta.get("content")

        # Case 1: Initial empty content chunk before thinking starts
        # e.g., delta: {"content": "", "role": "assistant"}
        if c == "" and not self.has_started_content:
            # Keep chunk as is or populate role, but do not mark content started
            return [chunk]

        # Case 2: Mixed chunk containing both reasoning and non-empty content
        # e.g., delta: {"reasoning": ".", "content": "9."}
        if r and c:
            if not self.has_started_content:
                # Frame 1: Flush out the thinking part
                chunk1 = json.loads(json.dumps(chunk))
                chunk1["choices"][0]["delta"] = {
                    k: v for k, v in delta.items() if k != "content"
                }
                chunk1["choices"][0]["delta"]["reasoning"] = r
                chunk1["choices"][0]["delta"]["reasoning_content"] = r

                # Frame 2: Start the content part
                chunk2 = json.loads(json.dumps(chunk))
                chunk2["choices"][0]["delta"] = {
                    k: v for k, v in delta.items() if k not in ("reasoning", "reasoning_content")
                }
                chunk2["choices"][0]["delta"]["content"] = c
                self.has_started_content = True
                return [chunk1, chunk2]
            else:
                # Already started content; merge reasoning into content smoothly
                chunk["choices"][0]["delta"].pop("reasoning", None)
                chunk["choices"][0]["delta"].pop("reasoning_content", None)
                chunk["choices"][0]["delta"]["content"] = f"{r}{c}"
                return [chunk]

        # Case 3: Pure reasoning chunk
        if r and not c:
            if not self.has_started_content:
                # Standard thinking chunk: dual-write both fields for complete client compatibility
                delta["reasoning"] = r
                delta["reasoning_content"] = r
                return [chunk]
            else:
                # Jitter anomaly: reasoning appeared after content had already started!
                # Monotonic state guard: convert reasoning into content to prevent UI flutter
                delta.pop("reasoning", None)
                delta.pop("reasoning_content", None)
                delta["content"] = r
                return [chunk]

        # Case 4: Pure content chunk
        if c:
            self.has_started_content = True
            delta.pop("reasoning", None)
            delta.pop("reasoning_content", None)
            return [chunk]

        return [chunk]


async def normalize_sse_stream(
    raw_stream: AsyncGenerator[bytes, None]
) -> AsyncGenerator[bytes, None]:
    """
    Consumes raw byte stream from upstream, parses SSE lines, normalizes chunks,
    and yields sanitized SSE byte chunks.
    """
    normalizer = StreamReasoningNormalizer()
    buffer = ""

    async for raw_bytes in raw_stream:
        buffer += raw_bytes.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            if line.startswith("data: "):
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    yield b"data: [DONE]\n\n"
                    continue
                try:
                    chunk_obj = json.loads(data_str)
                    normalized_chunks = normalizer.process_chunk_dict(chunk_obj)
                    for norm in normalized_chunks:
                        out_line = f"data: {json.dumps(norm, ensure_ascii=False)}\n\n"
                        yield out_line.encode("utf-8")
                except Exception as e:
                    logger.debug("Passthrough malformed SSE data line: %s (%s)", line, e)
                    yield f"{line}\n\n".encode("utf-8")
            else:
                # Comments (: ping) or other SSE fields
                yield f"{line}\n\n".encode("utf-8")

    # Flush remaining buffer if any
    if buffer.strip():
        line = buffer.strip()
        if line.startswith("data: "):
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                yield b"data: [DONE]\n\n"
            else:
                try:
                    chunk_obj = json.loads(data_str)
                    for norm in normalizer.process_chunk_dict(chunk_obj):
                        out_line = f"data: {json.dumps(norm, ensure_ascii=False)}\n\n"
                        yield out_line.encode("utf-8")
                except Exception:
                    yield f"{line}\n\n".encode("utf-8")
