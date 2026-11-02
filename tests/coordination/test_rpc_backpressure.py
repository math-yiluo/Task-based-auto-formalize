from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest

from formal_geyser.coordination.models import CodexConfig
from formal_geyser.coordination.runtime.codex import CodexHarness, ToolResult
from formal_geyser.coordination.runtime.process import OwnedProcessHost
from formal_geyser.coordination.runtime.rpc import (
    JsonRpcClient,
    RpcClosed,
    RpcError,
    RpcReply,
)


class MemoryWriter:
    def __init__(self):
        self.sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.frames: list[dict[str, Any]] = []

    def write(self, frame: bytes) -> None:
        message = json.loads(frame)
        self.frames.append(message)
        self.sent.put_nowait(message)

    async def drain(self) -> None:
        await asyncio.sleep(0)


def memory_peer(**kwargs):
    host = SimpleNamespace(
        stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader(), stdin=MemoryWriter())
    return host, JsonRpcClient(cast(OwnedProcessHost, host), **kwargs)


def feed(host, messages) -> None:
    host.stdout.feed_data(b"".join(
        json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        for message in messages))


@pytest.mark.parametrize("lane", ["request", "notification"])
async def test_burst_is_lossless_while_callback_waits_for_response(lane):
    seen, delivered = [], []

    async def callback(method, params):
        assert method == "event"
        if params["sequence"] == 0:
            assert await rpc.request("nested") == "accepted"
        seen.append(params)

        async def after_send():
            delivered.append(params["sequence"])

        return RpcReply(params, after_send=after_send)

    host, rpc = memory_peer(
        queue_limit=4, max_frame_bytes=1024,
        **{"request_handler" if lane == "request" else "notification_handler": callback})

    def event(sequence):
        return {
            "method": "event", "params": {"sequence": sequence, "text": "Δ测试"},
            **({"id": f"server-{sequence}"} if lane == "request" else {}),
        }

    try:
        async with asyncio.timeout(10):
            feed(host, [event(0)])
            nested = await host.stdin.sent.get()
            assert nested["method"] == "nested"
            barrier = asyncio.create_task(rpc.request("barrier"))
            barrier_frame = await host.stdin.sent.get()
            feed(host, [event(i) for i in range(1, 1025)] + [
                {"id": barrier_frame["id"], "result": "reader reached response"}])
            assert await barrier == "reader reached response"
            queue = rpc._requests if lane == "request" else rpc._notifications
            assert len(queue._memory) <= 4
            assert queue._spilled > 1000
            spill = queue._spill
            draining = asyncio.create_task(rpc.drain_callbacks())
            await asyncio.sleep(0)
            assert not draining.done()
            # No callback can consume its backlog until the shared reader routes
            # this nested response. Blocking queue.put() would deadlock here.
            feed(host, [{"id": nested["id"], "result": "accepted"}])
            await draining
            assert seen == [event(i)["params"] for i in range(1025)]
            if lane == "request":
                assert delivered == list(range(1025))
                assert [m["id"] for m in host.stdin.frames if "result" in m] == [
                    f"server-{i}" for i in range(1025)]
            assert rpc.failure is None
            assert spill.closed
            assert queue._spill is None
            # A second batch must follow the first after disk storage is reclaimed.
            feed(host, [event(1025)])
            barrier = asyncio.create_task(rpc.request("barrier-again"))
            while (frame := await host.stdin.sent.get()).get("method") != "barrier-again":
                pass
            feed(host, [{"id": frame["id"], "result": None}])
            await barrier
            await rpc.drain_callbacks()
            assert seen[-1] == event(1025)["params"]
    finally:
        await rpc.close()


async def test_buffered_frames_yield_to_immediate_callbacks(monkeypatch):
    seen = []

    async def callback(method, params):
        seen.append(params["sequence"])

    def unexpected_spill(**kwargs):
        raise AssertionError("Buffered reader starved an immediately available callback")

    monkeypatch.setattr(
        "formal_geyser.coordination.runtime.rpc.tempfile.TemporaryFile",
        unexpected_spill)
    host, rpc = memory_peer(notification_handler=callback, queue_limit=2)
    try:
        async with asyncio.timeout(10):
            barrier = asyncio.create_task(rpc.request("barrier"))
            request = await host.stdin.sent.get()
            feed(host, [{"method": "event", "params": {"sequence": i}}
                        for i in range(4096)] + [{"id": request["id"], "result": True}])
            assert await barrier is True
            await rpc.drain_callbacks()
            assert seen == list(range(4096))
            assert rpc.failure is None
    finally:
        await rpc.close()


async def test_eof_drains_spilled_usage_and_terminal_in_order():
    release = asyncio.Event()
    seen = []

    async def callback(method, params):
        await release.wait()
        seen.append((method, params))

    host, rpc = memory_peer(notification_handler=callback, queue_limit=2)
    expected = [("thread/tokenUsage/updated", {"totalTokens": i}) for i in range(400)]
    expected.append(("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}))
    try:
        async with asyncio.timeout(10):
            barrier = asyncio.create_task(rpc.request("barrier"))
            request = await host.stdin.sent.get()
            feed(host, [{"method": method, "params": params} for method, params in expected]
                 + [{"id": request["id"], "result": True}])
            assert await barrier is True
            assert rpc._notifications._spilled > 256
            host.stdout.feed_eof()
            host.stderr.feed_eof()
            assert isinstance(await rpc.wait_failure(), RpcClosed)
            await rpc.finish_receiving()
            draining = asyncio.create_task(rpc.drain_callbacks())
            await asyncio.sleep(0)
            assert not draining.done()
            release.set()
            await draining
            assert seen == expected
    finally:
        await rpc.close()


@pytest.mark.parametrize("cancel_close", [False, True])
async def test_close_cleans_spill_and_releases_pending_request_and_join(cancel_close):
    cancelling = asyncio.Event()

    async def callback(method, params):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if cancel_close:
                cancelling.set()
                await asyncio.Event().wait()
            raise

    host, rpc = memory_peer(notification_handler=callback, queue_limit=2)
    async with asyncio.timeout(10):
        try:
            barrier = asyncio.create_task(rpc.request("barrier"))
            request = await host.stdin.sent.get()
            feed(host, [{"method": "event", "params": {"sequence": i}} for i in range(400)]
                 + [{"id": request["id"], "result": True}])
            assert await barrier is True
            spill = rpc._notifications._spill
            assert spill is not None
            pending = asyncio.create_task(rpc.request("unanswered", timeout=3600))
            await host.stdin.sent.get()
            draining = asyncio.create_task(rpc.drain_callbacks())
            await asyncio.sleep(0)
            closing = asyncio.create_task(rpc.close())
            if cancel_close:
                await cancelling.wait()
                closing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await closing
            else:
                await closing
            with pytest.raises(RpcClosed):
                await pending
            with pytest.raises(RpcClosed, match="before callbacks drained"):
                await draining
            assert spill.closed
            assert all(task.done() for task in rpc._tasks)
            assert not rpc._pending
            await rpc.close()
        finally:
            await rpc.close()


@pytest.mark.parametrize("failure", ["create", "write", "short_write", "flush", "read"])
async def test_spill_storage_failure_fails_closed(monkeypatch, failure):
    import tempfile

    release = asyncio.Event()
    files = []
    temporary_file = tempfile.TemporaryFile

    async def callback(method, params):
        await release.wait()

    class FailedSpill:
        def __init__(self, file):
            self.file = file

        def __getattr__(self, name):
            return getattr(self.file, name)

        def write(self, frame):
            if failure == "write":
                raise OSError("fixture disk full")
            if failure == "short_write":
                return 0
            return self.file.write(frame)

        def flush(self):
            if failure == "flush":
                raise OSError("fixture disk full")
            return self.file.flush()

        def readline(self, size):
            if failure == "read":
                raise OSError("fixture disk unavailable")
            return self.file.readline(size)

    def unavailable_storage(**kwargs):
        if failure == "create":
            raise OSError("fixture disk full")
        file = temporary_file(**kwargs)
        files.append(file)
        return FailedSpill(file)

    monkeypatch.setattr(
        "formal_geyser.coordination.runtime.rpc.tempfile.TemporaryFile",
        unavailable_storage)
    host, rpc = memory_peer(notification_handler=callback, queue_limit=1)
    try:
        async with asyncio.timeout(10):
            pending = asyncio.create_task(rpc.request("unanswered"))
            await host.stdin.sent.get()
            feed(host, [{"method": "event", "params": {"sequence": i}} for i in range(4)])
            if failure == "read":
                barrier = asyncio.create_task(rpc.request("barrier"))
                request = await host.stdin.sent.get()
                feed(host, [{"id": request["id"], "result": True}])
                assert await barrier is True
                release.set()
            with pytest.raises(RpcError, match="spill storage failed"):
                await pending
            assert isinstance(rpc.failure.__cause__, OSError)
    finally:
        await rpc.close()
    assert all(file.closed for file in files)


async def test_resumed_turn_burst_preserves_native_output_usage_and_tool_receipt(tmp_path):
    script = tmp_path / "peer.py"
    script.write_text("""
import json,sys
def emit(value):
    print(json.dumps(value),flush=True)
def event(method,params):
    emit({'method':method,'params':{'threadId':'saved-thread','turnId':'turn-1',**params}})
for line in sys.stdin:
    message=json.loads(line)
    method=message.get('method')
    if method=='initialize':
        emit({'id':message['id'],'result':{'userAgent':'Codex Desktop/0.160.0'}})
    elif method=='thread/resume':
        for i in range(800):
            event('native/history',{'sequence':i})
        emit({'id':message['id'],'result':{'thread':{'id':'saved-thread','turns':[]}}})
    elif method=='turn/start':
        emit({'id':message['id'],'result':{'turn':{'id':'turn-1','status':'inProgress'}}})
        for i in range(800):
            event('item/agentMessage/delta',{'itemId':'answer','delta':'x'})
        event('thread/tokenUsage/updated',{'tokenUsage':{'total':{'totalTokens':100}}})
        event('item/completed',{'item':{'id':'answer','type':'agentMessage',
                                    'text':'x'*800,'phase':'finalAnswer'}})
        emit({'id':'tool-1','method':'item/tool/call','params':{
            'threadId':'saved-thread','turnId':'turn-1','callId':'call-1',
            'tool':'taskhub_call','arguments':{}}})
    elif method=='thread/read':
        emit({'id':message['id'],'result':{'thread':{'id':'saved-thread','turns':[]}}})
    elif message.get('id')=='tool-1' and 'result' in message:
        assert message['result']['success'] is True
        event('thread/tokenUsage/updated',{'tokenUsage':{'total':{'totalTokens':110}}})
        event('turn/completed',{'turn':{'id':'turn-1','status':'completed'}})
""", encoding="utf-8")
    harness = CodexHarness(
        CodexConfig(stop_grace_seconds=2), command=[sys.executable, "-u", str(script)])
    native, usages, starts, receipts = [], [], [], []
    blocked = False

    async def native_event(method, params):
        nonlocal blocked
        native.append((method, params))
        if method == "item/agentMessage/delta" and not blocked:
            blocked = True
            result = await harness.rpc.request("thread/read", {"threadId": "saved-thread"})
            assert result["thread"]["id"] == "saved-thread"
            assert harness.rpc._notifications._spilled > 256

    async def tool(*args):
        async def after_send():
            receipts.append("delivered")

        return ToolResult({"ok": True}, after_send=after_send)

    async def usage(sample):
        usages.append(sample)

    async def started(turn_id):
        starts.append(turn_id)

    try:
        async with asyncio.timeout(15):
            session = await harness.open(
                workspace=tmp_path, thread_id="saved-thread", tools=[], instructions="fixture")
            harness.set_native_handler(native_event)
            outcome = await harness.run_turn(
                "fixture", on_tool=tool, on_usage=usage, on_started=started)
            assert session.thread_id == "saved-thread"
            assert outcome.status == "completed"
            assert outcome.terminal_observed and outcome.process_quiet
            assert outcome.error is None
            assert outcome.final_output == "x" * 800
            assert usages == [{"totalTokens": 100}, {"totalTokens": 110}]
            assert starts == ["turn-1"]
            assert receipts == ["delivered"]
            assert len([m for m, _ in native if m == "item/agentMessage/delta"]) == 800
    finally:
        await harness.close()
