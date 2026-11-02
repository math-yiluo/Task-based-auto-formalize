"""Bounded asynchronous newline-delimited JSON-RPC transport."""
from __future__ import annotations

import asyncio
import json
import tempfile
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, BinaryIO, Callable, cast

from .process import OwnedProcessHost


def _reject_constant(value: str) -> None:
    raise ValueError(f"Nonstandard JSON constant: {value}")


class RpcError(RuntimeError):
    pass


class RpcClosed(RpcError):
    pass


class RpcProtocolError(RpcError):
    pass


class RpcTimeout(RpcError):
    def __init__(self, method: str):
        super().__init__(f"RPC request timed out; remote outcome is unknown: {method}")
        self.method = method


class RpcRemoteError(RpcError):
    def __init__(self, error: Any):
        super().__init__(f"Remote RPC error: {error}")
        self.error = error


@dataclass
class RpcReply:
    result: Any
    after_send: Callable[[], Awaitable[None]] | None = None
    ignore_closed_receiver: bool = False


RequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class _IncomingQueue:
    """Serial callback FIFO with bounded memory and private overflow storage.

    Backpressure cannot stop the shared pipe reader: a callback may be waiting
    for an RPC response behind this backlog. Keep at most ``memory_limit``
    decoded frames in memory, then spill the original, already validated frames
    until that backlog drains. Responses never enter this queue.
    """

    def __init__(self, memory_limit: int, max_frame_bytes: int):
        self._memory_limit = memory_limit
        self._max_frame_bytes = max_frame_bytes
        self._memory: deque[dict[str, Any]] = deque()
        self._spill: BinaryIO | None = None
        self._spill_position = 0
        self._spilled = 0
        self._unfinished = 0
        self._available = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()
        self._closed = False

    def put_nowait(self, message: dict[str, Any], frame: bytes) -> None:
        if self._closed:
            raise RpcClosed("Incoming RPC queue is closed")
        if self._spill is None and len(self._memory) < self._memory_limit:
            self._memory.append(message)
        else:
            try:
                if self._spill is None:
                    self._spill = cast(BinaryIO, tempfile.TemporaryFile(
                        mode="w+b", prefix="taskhub-rpc-"))
                self._spill.seek(0, 2)
                if self._spill.write(frame) != len(frame):
                    raise OSError("Incomplete spill write")
                self._spill.flush()
                self._spilled += 1
            except OSError as exc:
                raise RpcError("Incoming RPC spill storage failed") from exc
        self._unfinished += 1
        self._drained.clear()
        self._available.set()

    async def get(self) -> dict[str, Any]:
        await self._available.wait()
        if self._closed:
            raise RpcClosed("Incoming RPC queue is closed")
        if self._memory:
            message = self._memory.popleft()
        else:
            assert self._spill is not None
            try:
                self._spill.seek(self._spill_position)
                frame = self._spill.readline(self._max_frame_bytes + 1)
                if len(frame) > self._max_frame_bytes or not frame.endswith(b"\n"):
                    raise ValueError("Invalid spill frame")
                self._spill_position = self._spill.tell()
                message = cast(dict[str, Any], json.loads(frame))
                self._spilled -= 1
                if not self._spilled:
                    self._close_spill()
            except (OSError, ValueError) as exc:
                raise RpcError("Incoming RPC spill storage failed") from exc
        if not self._memory and not self._spilled:
            self._available.clear()
        return message

    def task_done(self) -> None:
        if self._unfinished <= 0:
            raise ValueError("task_done() called too many times")
        self._unfinished -= 1
        if not self._unfinished:
            self._drained.set()

    async def join(self) -> None:
        await self._drained.wait()
        if self._closed and self._unfinished:
            raise RpcClosed("RPC client closed before callbacks drained")

    def _close_spill(self) -> None:
        spill, self._spill = self._spill, None
        self._spill_position = 0
        if spill is not None:
            spill.close()

    def close(self) -> None:
        self._closed = True
        self._memory.clear()
        self._spilled = 0
        self._available.set()
        self._drained.set()
        self._close_spill()


class JsonRpcClient:
    def __init__(
        self, host: OwnedProcessHost, *, request_handler: RequestHandler | None = None,
        notification_handler: NotificationHandler | None = None,
        max_frame_bytes: int = 8 * 1024 * 1024, queue_limit: int = 256,
        request_timeout: float = 30.0, stderr_bytes: int = 65536,
    ):
        if max_frame_bytes < 1 or queue_limit < 1 or request_timeout <= 0 or stderr_bytes < 1:
            raise ValueError("RPC bounds and timeout must be positive")
        self.host = host
        self.request_handler = request_handler
        self.notification_handler = notification_handler
        self.max_frame_bytes = max_frame_bytes
        self.request_timeout = request_timeout
        self.stderr_bytes = stderr_bytes
        self.stderr_tail = bytearray()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._retired: deque[int] = deque(maxlen=1024)
        self._outgoing: asyncio.Queue = asyncio.Queue(queue_limit)
        self._requests = _IncomingQueue(queue_limit, max_frame_bytes)
        self._notifications = _IncomingQueue(queue_limit, max_frame_bytes)
        self._failure = asyncio.get_running_loop().create_future()
        self._closed = False
        self._callback_error: BaseException | None = None
        self._tasks = [
            asyncio.create_task(self._reader(), name="rpc-reader"),
            asyncio.create_task(self._writer(), name="rpc-writer"),
            asyncio.create_task(self._stderr(), name="rpc-stderr"),
            asyncio.create_task(self._serve(), name="rpc-server-requests"),
            asyncio.create_task(self._notify(), name="rpc-notifications"),
        ]

    @property
    def failure(self) -> BaseException | None:
        return self._failure.result() if self._failure.done() else None

    async def wait_failure(self) -> BaseException:
        return await asyncio.shield(self._failure)

    def _fail(self, exc: BaseException) -> None:
        if not self._failure.done():
            self._failure.set_result(exc)
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        while not self._outgoing.empty():
            _, ack = self._outgoing.get_nowait()
            if not ack.done():
                ack.set_exception(exc)
            self._outgoing.task_done()

    def _check(self) -> None:
        if self._closed:
            raise RpcClosed("RPC client is closed")
        if self.failure is not None:
            raise self.failure

    async def _send(self, message: dict[str, Any]) -> None:
        self._check()
        try:
            frame = (json.dumps(message, ensure_ascii=False, separators=(",", ":"),
                                allow_nan=False) + "\n").encode("utf-8")
        except (ValueError, TypeError) as exc:
            raise RpcProtocolError("Message is not valid JSON") from exc
        if len(frame) > self.max_frame_bytes:
            raise RpcProtocolError("Outgoing frame exceeds the byte limit")
        ack = asyncio.get_running_loop().create_future()
        try:
            self._outgoing.put_nowait((frame, ack))
        except asyncio.QueueFull as exc:
            raise RpcProtocolError("Outgoing RPC queue is full") from exc
        try:
            await ack
        finally:
            if not ack.done():
                ack.cancel()

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None,
    ) -> Any:
        self._check()
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(timeout if timeout is not None else self.request_timeout):
                await self._send({"id": request_id, "method": method, "params": params or {}})
                return await future
        except TimeoutError as exc:
            raise RpcTimeout(method) from exc
        finally:
            self._pending.pop(request_id, None)
            self._retired.append(request_id)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # Retrieve a simultaneous failure even when send itself failed.
                future.exception()

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def _reader(self) -> None:
        try:
            while True:
                # readline() need not suspend for buffered (or retired) frames.
                # Give callbacks and writers a chance to run during every burst.
                await asyncio.sleep(0)
                try:
                    line = await self.host.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    raise RpcProtocolError("Incoming frame exceeds the byte limit") from exc
                if not line:
                    raise RpcClosed("RPC stdout reached EOF")
                if len(line) > self.max_frame_bytes:
                    raise RpcProtocolError("Incoming frame exceeds the byte limit")
                if not line.endswith(b"\n"):
                    raise RpcProtocolError("RPC stream ended inside a frame")
                if not line.strip():
                    continue
                try:
                    message = json.loads(line, parse_constant=_reject_constant)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise RpcProtocolError("Malformed RPC JSON frame") from exc
                if not isinstance(message, dict):
                    raise RpcProtocolError("RPC frames must be objects")
                if "method" in message:
                    if not isinstance(message["method"], str):
                        raise RpcProtocolError("RPC method must be a string")
                    params = message.get("params", {})
                    if not isinstance(params, dict):
                        raise RpcProtocolError("RPC params must be an object")
                    queue = self._requests if "id" in message else self._notifications
                    if "id" in message and (
                        isinstance(message["id"], bool)
                        or not isinstance(message["id"], (str, int))
                    ):
                        raise RpcProtocolError("Invalid server request ID")
                    queue.put_nowait(message, line)
                else:
                    request_id = message.get("id")
                    if isinstance(request_id, bool) or not isinstance(request_id, int):
                        raise RpcProtocolError("Invalid response ID")
                    if ("result" in message) == ("error" in message):
                        raise RpcProtocolError("RPC response needs exactly result or error")
                    future = self._pending.get(request_id)
                    if future is None:
                        if request_id in self._retired:
                            continue
                        raise RpcProtocolError("Response has an unknown request ID")
                    if future.done():
                        continue
                    if "error" in message:
                        future.set_exception(RpcRemoteError(message["error"]))
                    else:
                        future.set_result(message["result"])
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fail(exc)

    async def _writer(self) -> None:
        active = None
        try:
            while True:
                frame, ack = await self._outgoing.get()
                active = ack
                try:
                    self.host.stdin.write(frame)
                    await self.host.stdin.drain()
                    if not ack.done():
                        ack.set_result(None)
                finally:
                    self._outgoing.task_done()
                active = None
        except asyncio.CancelledError:
            if active is not None and not active.done():
                active.cancel()
            raise
        except BaseException as exc:
            # All observers must see the same transport-close classification.
            # A raw pipe reset here could otherwise override an observed terminal
            # while sending the discarded reply to an interrupted local tool.
            closed = RpcClosed(f"RPC writer failed: {type(exc).__name__}")
            if active is not None and not active.done():
                active.set_exception(closed)
            self._fail(closed)

    async def _stderr(self) -> None:
        try:
            while chunk := await self.host.stderr.read(8192):
                self.stderr_tail.extend(chunk)
                if len(self.stderr_tail) > self.stderr_bytes:
                    del self.stderr_tail[:-self.stderr_bytes]
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fail(RpcClosed(f"RPC stderr drainer failed: {type(exc).__name__}"))

    async def _serve(self) -> None:
        try:
            while True:
                message = await self._requests.get()
                try:
                    if self.request_handler is None:
                        await self._send({"id": message["id"], "error": {
                            "code": -32601, "message": "Unsupported server request"}})
                        continue
                    try:
                        reply = await self.request_handler(
                            message["method"], message.get("params", {}))
                    except Exception as exc:
                        await self._send({"id": message["id"], "error": {
                            "code": -32603, "message": type(exc).__name__ + ": " + str(exc)[:1000]}})
                        continue
                    if not isinstance(reply, RpcReply):
                        reply = RpcReply(reply)
                    try:
                        await self._send({"id": message["id"], "result": reply.result})
                    except RpcClosed:
                        if not reply.ignore_closed_receiver:
                            raise
                        # An explicitly cancelled local tool has no result to admit.
                        # EOF still fails the turn unless its native terminal was observed.
                        continue
                    if reply.after_send is not None:
                        await reply.after_send()
                finally:
                    self._requests.task_done()
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._callback_error = exc
            self._fail(exc)

    async def _notify(self) -> None:
        try:
            while True:
                message = await self._notifications.get()
                try:
                    if self.notification_handler is not None:
                        await self.notification_handler(
                            message["method"], message.get("params", {}))
                finally:
                    self._notifications.task_done()
                # A drained spill can contain many immediately handled frames.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._callback_error = exc
            self._fail(exc)

    async def finish_receiving(self, *, timeout: float = 5.0) -> None:
        # After the owned tree dies, consume every buffered stdout/stderr byte.
        try:
            async with asyncio.timeout(timeout):
                await asyncio.shield(asyncio.gather(self._tasks[0], self._tasks[2]))
        except TimeoutError as exc:
            raise TimeoutError(
                "RPC shutdown timed out waiting for stdout/stderr EOF "
                f"(stdout_done={self._tasks[0].done()}, stderr_done={self._tasks[2].done()})"
            ) from exc
        if self.failure is not None and not isinstance(self.failure, RpcClosed):
            raise self.failure

    async def drain_callbacks(self, *, timeout: float = 5.0) -> None:
        if self._callback_error is not None:
            raise self._callback_error
        try:
            async with asyncio.timeout(timeout):
                await self._requests.join()
                await self._notifications.join()
        except TimeoutError as exc:
            if self._callback_error is not None:
                raise self._callback_error
            raise TimeoutError(
                "RPC shutdown timed out draining callbacks "
                f"(requests={self._requests._unfinished}, "
                f"notifications={self._notifications._unfinished})"
            ) from exc
        if self._callback_error is not None:
            raise self._callback_error

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fail(RpcClosed("RPC client closed"))
        for task in self._tasks:
            task.cancel()
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            try:
                self._requests.close()
            finally:
                self._notifications.close()

