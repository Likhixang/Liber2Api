"""Offline regression coverage for completion framing and upstream ownership."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import anyio

from app.libertai_client import LibertAIClient
from app.model_resolver import resolve_upstream_model
from app.stream_normalizer import StreamReasoningNormalizer, normalize_sse_stream


def chunk(delta, index=0, **choice_fields):
    return {"id": "completion-test", "choices": [{"index": index, "delta": delta, **choice_fields}]}


def choices(frames, index=0):
    return [c for frame in frames for c in frame.get("choices", []) if c.get("index", 0) == index]


def text(frames, field, index=0):
    return "".join((c.get("delta") or {}).get(field) or "" for c in choices(frames, index))


def parse_events(raw):
    events = []
    for event in raw.decode().replace("\r\n", "\n").split("\n\n"):
        data = []
        for line in event.splitlines():
            if line.startswith("data:"):
                value = line[5:]
                data.append(value[1:] if value.startswith(" ") else value)
        if data:
            value = "\n".join(data)
            events.append(value if value == "[DONE]" else json.loads(value))
    return events


async def normalized(parts):
    async def raw():
        for part in parts:
            yield part
    return b"".join([part async for part in normalize_sse_stream(raw())])


UTF8_FRAME = ("data: " + json.dumps(chunk({"content": "中文🙂", "tool_calls": [{"index": 0, "function": {"arguments": '{"位置":"上海"}'}}]}), ensure_ascii=False) + "\n\n").encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", range(len(UTF8_FRAME) + 1))
async def test_utf8_survives_every_network_split(cut):
    events = parse_events(await normalized([UTF8_FRAME[:cut], UTF8_FRAME[cut:]]))
    assert text(events, "content") == "中文🙂"
    assert choices(events)[0]["delta"]["tool_calls"][0]["function"]["arguments"] == '{"位置":"上海"}'


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("spacing", ["", " "])
async def test_sse_multiline_event_fields_and_arbitrary_byte_boundaries(ending, spacing):
    lines = [": heartbeat", "id: 42", "event: message", "retry: 1000", 'data:' + spacing + '{"choices":[', 'data:' + spacing + '{"index":1,"delta":{"reasoning":"思考","content":""}}]}', "", "data:[DONE]", "", ""]
    wire = ending.join(lines).encode()
    output = await normalized([wire[i:i + 1] for i in range(len(wire))])
    events = parse_events(output)
    assert events[-1] == "[DONE]"
    assert len(events) == 2
    assert text(events[:-1], "reasoning_content", 1) == "思考"
    first = output.decode().split("\n\n")[0]
    for field in (": heartbeat", "id: 42", "event: message", "retry: 1000"):
        assert field in first


@pytest.mark.asyncio
async def test_malformed_event_is_not_split_and_nonobject_json_passes_through():
    wire = b'event: custom\ndata: broken\ndata: data\n\ndata: [1,2]\n\n'
    output = await normalized([wire])
    assert b'event: custom\ndata: broken\ndata: data\n\n' in output
    assert b'data: [1,2]\n\n' in output


def test_mixed_choices_metadata_and_tools_emitted_exactly_once_without_mutation():
    normalizer = StreamReasoningNormalizer()
    original = {"id": "test", "usage": {"completion_tokens": 3}, "choices": [
        {"index": 0, "delta": {"role": "assistant", "reasoning": "R", "content": "A", "tool_calls": [{"index": 0, "function": {"arguments": '{"a":'}}]}, "finish_reason": "tool_calls", "logprobs": {"content": ["one"]}},
        {"index": 1, "delta": {"content": "B"}, "finish_reason": None},
    ]}
    before = copy.deepcopy(original)
    frames = normalizer.process_chunk_dict(original)
    assert original == before
    assert text(frames, "reasoning_content") == "R"
    assert text(frames, "content") == "A"
    assert text(frames, "content", 1) == "B"
    emitted = choices(frames)
    assert sum("role" in c["delta"] for c in emitted) == 1
    assert sum("tool_calls" in c["delta"] for c in emitted) == 1
    assert sum(c.get("finish_reason") == "tool_calls" for c in emitted) == 1
    assert "finish_reason" not in emitted[0] or emitted[0]["finish_reason"] is None
    assert emitted[-1]["finish_reason"] == "tool_calls"
    assert sum("logprobs" in c for c in emitted) == 1
    assert sum("usage" in frame for frame in frames) == 1
    assert frames[-1]["usage"] == original["usage"]


def test_interleaved_choice_indices_have_independent_monotonic_state():
    normalizer = StreamReasoningNormalizer()
    normalizer.process_chunk_dict(chunk({"content": "A"}, index=7))
    frames = normalizer.process_chunk_dict({"choices": [
        {"index": 9, "delta": {"reasoning_content": "R", "content": ""}},
        {"index": 7, "delta": {"reasoning": "late", "content": "B"}},
    ]})
    assert text(frames, "reasoning", 9) == "R"
    assert text(frames, "reasoning_content", 9) == "R"
    assert text(frames, "content", 7) == "lateB"
    assert text(frames, "reasoning", 7) == ""


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
def test_reasoning_with_empty_content_is_dual_written(field):
    frames = StreamReasoningNormalizer().process_chunk_dict(chunk({field: "thought", "content": "", "role": "assistant"}))
    assert text(frames, "reasoning") == "thought"
    assert text(frames, "reasoning_content") == "thought"
    assert choices(frames)[0]["delta"]["role"] == "assistant"


THINK_TEXT = "<think>secret</think>answer"


@pytest.mark.parametrize("cut", range(1, len(THINK_TEXT)))
def test_think_tags_survive_every_delta_split(cut):
    normalizer = StreamReasoningNormalizer()
    frames = normalizer.process_chunk_dict(chunk({"content": THINK_TEXT[:cut]}))
    frames += normalizer.process_chunk_dict(chunk({"content": THINK_TEXT[cut:]}, finish_reason="stop"))
    assert text(frames, "reasoning") == "secret"
    assert text(frames, "reasoning_content") == "secret"
    assert text(frames, "content") == "answer"
    assert choices(frames)[-1]["finish_reason"] == "stop"


def test_think_character_deltas_preserve_tools_role_and_terminal_metadata():
    normalizer = StreamReasoningNormalizer()
    frames = []
    for char in "<think>one</think><think>two</think>answer":
        frames.extend(normalizer.process_chunk_dict(chunk({"content": char}, index=4)))
    frames.extend(normalizer.process_chunk_dict(chunk({"content": "", "role": "assistant", "tool_calls": [{"index": 0}]}, index=4, finish_reason="tool_calls")))
    assert text(frames, "reasoning", 4) == "onetwo"
    assert text(frames, "content", 4) == "answer"
    assert sum("tool_calls" in c["delta"] for c in choices(frames, 4)) == 1
    assert choices(frames, 4)[-1]["finish_reason"] == "tool_calls"


def test_single_think_chunk_preserves_nontext_fields_once():
    original = chunk({"content": THINK_TEXT, "role": "assistant", "tool_calls": [{"index": 0}]}, finish_reason="stop")
    frames = StreamReasoningNormalizer().process_chunk_dict(original)
    assert text(frames, "content") == "answer"
    assert sum("role" in c["delta"] for c in choices(frames)) == 1
    assert sum("tool_calls" in c["delta"] for c in choices(frames)) == 1
    assert choices(frames)[-1]["finish_reason"] == "stop"
    assert sum(c.get("finish_reason") == "stop" for c in choices(frames)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", [b"", b"data: [DONE]\n\n"])
@pytest.mark.parametrize("value,field,expected", [("literal<thi", "content", "literal<thi"), ("<think>secret</thi", "reasoning_content", "secret</thi")])
async def test_partial_tag_flush_at_done_or_eof(tail, value, field, expected):
    wire = ("data:" + json.dumps(chunk({"content": value})) + "\n\n").encode() + tail
    frames = [e for e in parse_events(await normalized([wire])) if isinstance(e, dict)]
    assert text(frames, field) == expected


@pytest.mark.asyncio
async def test_partial_tag_flush_before_finish_and_unterminated_sse_eof():
    wire = ("data:" + json.dumps(chunk({"content": "<thi"})) + "\n\n" + "data:" + json.dumps(chunk({}, finish_reason="stop"))).encode()
    frames = parse_events(await normalized([wire]))
    assert text(frames, "content") == "<thi"
    assert choices(frames)[-1]["finish_reason"] == "stop"


def test_tag_extraction_respects_monotonic_state_and_choice_isolation():
    normalizer = StreamReasoningNormalizer()
    frames = normalizer.process_chunk_dict(chunk({"content": "answer<think>late</think>tail"}))
    frames += normalizer.process_chunk_dict(chunk({"content": "<thi"}, index=1))
    frames += normalizer.process_chunk_dict(chunk({"content": "nk>independent</think>done"}, index=1))
    assert text(frames, "content") == "answerlatetail"
    assert text(frames, "reasoning") == ""
    assert text(frames, "reasoning", 1) == "independent"


@pytest.mark.parametrize("model,payload,headers", [
    ("deepseek-v4.1-flash-1017-tee", {}, {}),
    ("deepseek-v4.1-flash-thinking-tee", {}, {}),
    ("deepseek-v4.1-flash", {"tee": True}, {}),
    ("unknown-model", {}, {"x-tee": "true"}),
    ("unknown-model", {}, {"X-TEE": " TRUE "}),
])
def test_explicit_tee_requests_fail_closed(model, payload, headers):
    with pytest.raises(ValueError, match="TEE"):
        resolve_upstream_model(model, payload, headers)


@pytest.mark.parametrize("payload", [{"tee": False}, {"tee": "false"}, {"tee": "0"}])
def test_false_tee_flags_do_not_enable_tee(payload):
    assert resolve_upstream_model("qwen3.8-27b", payload) == "qwen3.8-27b-thinking"


@pytest.mark.parametrize("value", [None, [], {}, 123, "yes", 2])
def test_invalid_tee_flags_are_rejected(value):
    with pytest.raises(ValueError, match="tee"):
        resolve_upstream_model("qwen3.8-27b", {"tee": value})


def test_supported_and_tee_only_models_keep_existing_routes():
    assert resolve_upstream_model("qwen3.8-27b-tee", {"reasoning_effort": "none"}) == "qwen3.8-27b-tee"
    assert resolve_upstream_model("qwen3.8-27b", {}, {"X-TEE": "true"}) == "qwen3.8-27b-tee-thinking"
    assert resolve_upstream_model("hermes-3-8b", {"tee": False}) == "hermes-3-8b-tee"


class RecordingStream(httpx.AsyncByteStream):
    def __init__(self, parts=(), error=None, block=False, close_error=None):
        self.parts = parts
        self.error = error
        self.block = block
        self.close_error = close_error
        self.closed = False
        self.entered = asyncio.Event()

    async def __aiter__(self):
        for part in self.parts:
            yield part
        self.entered.set()
        if self.block:
            await asyncio.Event().wait()
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


def upstream_client(monkeypatch, stream=None, status=200, error=None):
    responses = []
    def handler(request):
        if error:
            raise error
        response = httpx.Response(status, stream=stream)
        responses.append(response)
        return response
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = LibertAIClient(api_base="https://offline.invalid/v1")
    monkeypatch.setattr(api, "_get_client", lambda **kwargs: upstream)
    return api, upstream, responses


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout, asyncio.CancelledError])
async def test_send_failures_always_close_client(monkeypatch, error_type):
    api, upstream, _ = upstream_client(monkeypatch, error=error_type("offline failure"))
    with pytest.raises(error_type):
        await api.forward_completion_stream("dummy", {})
    assert upstream.is_closed


@pytest.mark.asyncio
async def test_build_request_failure_always_closes_client(monkeypatch):
    api, upstream, _ = upstream_client(monkeypatch)
    monkeypatch.setattr(upstream, "build_request", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("invalid request")))
    with pytest.raises(ValueError):
        await api.forward_completion_stream("dummy", {})
    assert upstream.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ReadError, asyncio.CancelledError])
async def test_error_body_read_failures_close_response_and_client(monkeypatch, error_type):
    source = RecordingStream(error=error_type("offline read failure"))
    api, upstream, responses = upstream_client(monkeypatch, source, 503)
    with pytest.raises(error_type):
        await api.forward_completion_stream("dummy", {})
    assert source.closed
    assert responses[0].is_closed
    assert upstream.is_closed


@pytest.mark.asyncio
async def test_successful_error_body_is_available_after_upstream_close(monkeypatch):
    source = RecordingStream([b'{"detail":"offline error"}'])
    api, upstream, responses = upstream_client(monkeypatch, source, 429)
    status, result = await api.forward_completion_stream("dummy", {})
    assert status == 429
    assert source.closed and responses[0].is_closed and upstream.is_closed
    assert b"".join([part async for part in result]) == b'{"detail":"offline error"}'


@pytest.mark.asyncio
async def test_unstarted_returned_stream_can_be_closed_idempotently(monkeypatch):
    source = RecordingStream([UTF8_FRAME])
    api, upstream, responses = upstream_client(monkeypatch, source)
    status, result = await api.forward_completion_stream("dummy", {})
    assert status == 200
    await result.aclose()
    await result.aclose()
    assert source.closed and responses[0].is_closed and upstream.is_closed
    assert [part async for part in result] == []


@pytest.mark.asyncio
async def test_partial_consumption_close_closes_all_layers(monkeypatch):
    source = RecordingStream([UTF8_FRAME, UTF8_FRAME])
    api, upstream, responses = upstream_client(monkeypatch, source)
    _, result = await api.forward_completion_stream("dummy", {})
    assert await anext(result)
    await result.aclose()
    assert source.closed and responses[0].is_closed and upstream.is_closed


@pytest.mark.asyncio
async def test_iteration_failure_propagates_without_replay_and_closes(monkeypatch):
    source = RecordingStream([UTF8_FRAME], error=httpx.ReadError("offline read failure"))
    api, upstream, responses = upstream_client(monkeypatch, source)
    _, result = await api.forward_completion_stream("dummy", {})
    assert text(parse_events(await anext(result)), "content") == "中文🙂"
    with pytest.raises(httpx.ReadError):
        await anext(result)
    assert source.closed and responses[0].is_closed and upstream.is_closed


@pytest.mark.asyncio
async def test_cancelled_iteration_closes_all_resources(monkeypatch):
    source = RecordingStream(block=True)
    api, upstream, responses = upstream_client(monkeypatch, source)
    _, result = await api.forward_completion_stream("dummy", {})
    task = asyncio.create_task(anext(result))
    await source.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source.closed and responses[0].is_closed and upstream.is_closed


@pytest.mark.asyncio
async def test_client_closed_even_when_response_close_raises(monkeypatch):
    source = RecordingStream(close_error=RuntimeError("offline close failure"))
    api, upstream, _ = upstream_client(monkeypatch, source)
    _, result = await api.forward_completion_stream("dummy", {})
    with pytest.raises(RuntimeError, match="close failure"):
        await result.aclose()
    assert upstream.is_closed


@pytest.mark.asyncio
async def test_normal_completion_closes_response_and_client(monkeypatch):
    source = RecordingStream([UTF8_FRAME, b'data:[DONE]\n\n'])
    api, upstream, responses = upstream_client(monkeypatch, source)
    _, result = await api.forward_completion_stream("dummy", {})
    assert parse_events(b"".join([part async for part in result]))[-1] == "[DONE]"
    assert source.closed and responses[0].is_closed and upstream.is_closed


@pytest.mark.asyncio
async def test_normalizer_closes_raw_generator_on_early_close():
    closed = []
    async def source():
        try:
            yield UTF8_FRAME
            yield UTF8_FRAME
        finally:
            closed.append(True)
    result = normalize_sse_stream(source())
    assert await anext(result)
    await result.aclose()
    assert closed == [True]


@pytest.mark.asyncio
async def test_anyio_cancel_scope_cleanup_is_shielded(monkeypatch):
    class YieldingCloseStream(RecordingStream):
        async def aclose(self):
            await anyio.sleep(0)
            await super().aclose()

    source = YieldingCloseStream(block=True)
    api, upstream, responses = upstream_client(monkeypatch, source)
    _, result = await api.forward_completion_stream("dummy", {})
    async with anyio.create_task_group() as tasks:
        async def consume():
            async for _ in result:
                pass
        tasks.start_soon(consume)
        await source.entered.wait()
        tasks.cancel_scope.cancel()
    assert source.closed and responses[0].is_closed and upstream.is_closed


@pytest.mark.asyncio
async def test_bom_and_comments_keep_event_boundaries():
    wire = b'\xef\xbb\xbf: ping\r\n\r\n' + UTF8_FRAME
    frames = parse_events(await normalized([wire[i:i + 1] for i in range(len(wire))]))
    assert text(frames, "content") == "中文🙂"


@pytest.mark.asyncio
async def test_usage_only_event_flushes_pending_prefix_before_usage():
    wire = ("data:" + json.dumps(chunk({"content": "<thi"})) + "\n\n" + 'data:{"choices":[],"usage":{"completion_tokens":3}}\n\n' + 'data:[DONE]\n\n').encode()
    frames = parse_events(await normalized([wire]))
    assert text(frames[:-1], "content") == "<thi"
    assert frames[-2] == {"choices": [], "usage": {"completion_tokens": 3}}


@pytest.mark.parametrize("delta", [None, "missing"])
def test_finish_without_delta_flushes_pending_prefix_first(delta):
    normalizer = StreamReasoningNormalizer()
    frames = normalizer.process_chunk_dict(chunk({"content": "<thi"}))
    terminal = {"index": 0, "finish_reason": "stop"}
    if delta is None:
        terminal["delta"] = None
    frames += normalizer.process_chunk_dict({"choices": [terminal]})
    assert text(frames, "content") == "<thi"
    assert choices(frames)[-1]["finish_reason"] == "stop"


def test_nonstreaming_reasoning_aliases_normalize_all_choices():
    response = {"choices": [
        {"message": {"reasoning": "first", "content": "A"}},
        {"message": {"reasoning_content": "second", "content": "B", "tool_calls": [{"id": "tool"}]}},
    ]}
    result = StreamReasoningNormalizer().normalize_json_response(response)
    assert result["choices"][0]["message"]["reasoning_content"] == "first"
    assert result["choices"][1]["message"]["reasoning"] == "second"
    assert result["choices"][1]["message"]["tool_calls"] == [{"id": "tool"}]
