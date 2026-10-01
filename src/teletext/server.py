"""USB companion service for the Teletext wire protocol."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import io
import logging
import os
import re
import shutil
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError, ServerConfig, load_config
from .content import ContentNotFound, ContentStore
from .protocol import (
    ProtocolError,
    Request,
    checksum,
    end_frame,
    error_frame,
    iter_data_frames,
    parse_request,
)

LOG = logging.getLogger(__name__)
_PREFIX = re.compile(r"^[0-9a-fA-F]{12}$")
_ID = re.compile(r"^[0-9a-fA-F]{4}$")


class SendFailed(RuntimeError):
    pass


class PortInUseError(RuntimeError):
    pass


@dataclass
class RetainedTransfer:
    sender: str
    request_id: str
    snapshot: Any
    count: int


@contextmanager
def exclusive_port(port: str):
    """Keep two Teletext processes from sharing one serial companion."""
    try:
        import fcntl
    except ImportError:  # Windows does not provide flock.
        yield
        return

    identity = hashlib.sha256(os.path.realpath(port).encode()).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"meshcore-teletext-{identity}.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.seek(0)
            owner = lock.read().strip()
            detail = f" (PID {owner})" if owner.isdecimal() else ""
            raise PortInUseError(f"Another Teletext server is using {port}{detail}") from exc
        try:
            lock.seek(0)
            lock.truncate()
            lock.write(str(os.getpid()))
            lock.flush()
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class TeletextServer:
    def __init__(self, mesh: Any, store: ContentStore):
        self.mesh = mesh
        self.store = store
        self._send_lock = asyncio.Lock()
        self._active: asyncio.Task[None] | None = None
        self._active_sender: str | None = None
        self._active_request_id: str | None = None
        self._active_count: int | None = None
        self._retained: RetainedTransfer | None = None
        self._pending_chunks: set[int] = set()
        self._ack_codes: set[str] = set()
        self._ack_event = asyncio.Event()
        self._subscriptions: list[Any] = []
        self._drain_task: asyncio.Task[None] | None = None
        self._drain_again = False
        self._last_drain_error: str | None = None

    def start(self) -> None:
        from meshcore import EventType

        self._subscriptions.append(self.mesh.subscribe(EventType.CONTACT_MSG_RECV, self._on_message))
        self._subscriptions.append(self.mesh.subscribe(EventType.ACK, self._on_ack))
        self._subscriptions.append(self.mesh.subscribe(EventType.MESSAGES_WAITING, self._on_waiting))
        self._schedule_drain()

    async def stop(self) -> None:
        if self._active is not None:
            self._active.cancel()
            await asyncio.gather(self._active, return_exceptions=True)
        if self._drain_task is not None:
            self._drain_task.cancel()
            await asyncio.gather(self._drain_task, return_exceptions=True)
        self._release_retained()
        for subscription in self._subscriptions:
            self.mesh.unsubscribe(subscription)
        self._subscriptions.clear()

    async def _on_ack(self, event: Any) -> None:
        code = str(event.payload.get("code", "")).lower()
        if code:
            self._ack_codes.add(code)
            self._ack_event.set()

    async def _on_waiting(self, event: Any) -> None:
        self._schedule_drain()

    def _schedule_drain(self) -> None:
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_again = True
        else:
            self._drain_again = False
            self._drain_task = asyncio.create_task(self._drain_messages())

    async def _drain_messages(self) -> None:
        from meshcore import EventType

        while True:
            async with self._send_lock:
                result = await self.mesh.commands.get_msg()
            if result is None or result.type == EventType.ERROR:
                detail = repr(getattr(result, "payload", None))
                if detail != self._last_drain_error:
                    LOG.warning("Could not fetch pending message (%s); retrying", detail)
                    self._last_drain_error = detail
                await asyncio.sleep(5)
                continue
            self._last_drain_error = None
            if result.type == EventType.NO_MORE_MSGS:
                if self._drain_again:
                    self._drain_again = False
                    continue
                return
            await asyncio.sleep(0.1)

    async def _on_message(self, event: Any) -> None:
        payload = event.payload
        sender = str(payload.get("pubkey_prefix", "")).lower()
        frame = payload.get("text")
        if not _PREFIX.fullmatch(sender) or not isinstance(frame, str) or not frame.startswith("T1"):
            return
        try:
            request = parse_request(frame)
        except (ProtocolError, ValueError):
            request_id = frame[3:7] if len(frame) >= 7 else ""
            if _ID.fullmatch(request_id):
                asyncio.create_task(self._send_error(sender, request_id.upper(), "BAD"))
            return
        if request.kind == "C":
            if (sender == self._active_sender and request.request_id == self._active_request_id
                    and self._active is not None):
                self._active.cancel()
            if (self._retained is not None and sender == self._retained.sender
                    and request.request_id == self._retained.request_id):
                self._release_retained()
            return
        if request.kind == "R":
            active_match = (self._active is not None and not self._active.done()
                            and sender == self._active_sender
                            and request.request_id == self._active_request_id)
            retained_match = (self._retained is not None and sender == self._retained.sender
                              and request.request_id == self._retained.request_id)
            count = self._active_count if active_match else self._retained.count if retained_match else None
            if not (active_match or retained_match) or (count is not None and request.sequence >= count):
                asyncio.create_task(self._send_error(sender, request.request_id, "BAD"))
                return
            self._pending_chunks.add(request.sequence)
            if not active_match:
                self._active_sender = sender
                self._active_request_id = request.request_id
                self._active_count = count
                self._active = asyncio.create_task(self._resend_retained())
            return
        LOG.info("Received %s request %s from %s", request.page or "index", request.request_id, sender)
        if self._active is not None and not self._active.done():
            if sender != self._active_sender:
                asyncio.create_task(self._send_error(sender, request.request_id, "BUSY"))
                return
            self._active.cancel()
        self._release_retained()
        self._pending_chunks.clear()
        self._active_sender = sender
        self._active_request_id = request.request_id
        self._active_count = None
        self._active = asyncio.create_task(self._serve(sender, request))

    async def _serve(self, sender: str, request: Request) -> None:
        current = asyncio.current_task()
        snapshot = None
        try:
            snapshot = await self._snapshot(request.page)
            count_task = asyncio.create_task(
                asyncio.to_thread(self._count_chunks, snapshot, request.request_id)
            )
            try:
                count = await asyncio.shield(count_task)
            except asyncio.CancelledError:
                await asyncio.shield(count_task)
                raise
            self._active_count = count
            count = 0
            crc = 0
            for frame, raw in self._frames(snapshot, request.request_id, self._active_count):
                await self._send_with_ack(sender, frame)
                crc = checksum(raw, crc)
                count += 1
            await self._send_with_ack(sender, end_frame(request.request_id, count, crc))
            while self._pending_chunks:
                sequence = min(self._pending_chunks)
                self._pending_chunks.remove(sequence)
                if sequence < count:
                    await self._send_with_ack(sender,
                        self._frame_at(snapshot, request.request_id, count, sequence))
            self._retained = RetainedTransfer(sender, request.request_id, snapshot, count)
            snapshot = None
            LOG.info("Served %s to %s in %d chunks", request.page or "index", sender, count)
        except asyncio.CancelledError:
            LOG.info("Cancelled transfer %s to %s", request.request_id, sender)
            raise
        except ContentNotFound:
            await self._send_error(sender, request.request_id, "NF")
        except (OSError, UnicodeError, ProtocolError, SendFailed) as exc:
            LOG.warning("Transfer %s failed: %s", request.request_id, exc)
            await self._send_error(sender, request.request_id, "IO")
        finally:
            if snapshot is not None:
                snapshot.close()
            if self._active is current:
                self._active = None
                self._active_sender = None
                self._active_request_id = None
                self._active_count = None

    def _frames(self, snapshot: Any, request_id: str, count: int):
        snapshot.seek(0)
        source = io.TextIOWrapper(snapshot, encoding="utf-8", errors="strict", newline="")
        try:
            yield from iter_data_frames(request_id, source, count)
        finally:
            source.detach()

    def _count_chunks(self, snapshot: Any, request_id: str) -> int:
        guess = 0
        while True:
            count = sum(1 for _ in self._frames(snapshot, request_id, guess))
            if count == guess:
                return count
            guess = count

    def _frame_at(self, snapshot: Any, request_id: str, count: int, sequence: int) -> str:
        for index, (frame, _) in enumerate(self._frames(snapshot, request_id, count)):
            if index == sequence:
                return frame
        raise ProtocolError("requested chunk absent from snapshot")

    async def _resend_retained(self) -> None:
        current = asyncio.current_task()
        try:
            while self._pending_chunks and self._retained is not None:
                sequence = min(self._pending_chunks)
                self._pending_chunks.remove(sequence)
                transfer = self._retained
                if sequence < transfer.count:
                    frame = self._frame_at(transfer.snapshot, transfer.request_id,
                                           transfer.count, sequence)
                    await self._send_with_ack(transfer.sender, frame)
        except asyncio.CancelledError:
            raise
        except (OSError, UnicodeError, ProtocolError, SendFailed) as exc:
            LOG.warning("Could not resend chunk: %s", exc)
        finally:
            if self._active is current:
                self._active = None
                self._active_sender = None
                self._active_request_id = None
                self._active_count = None

    def _release_retained(self) -> None:
        if self._retained is not None:
            self._retained.snapshot.close()
            self._retained = None

    async def _snapshot(self, page: int | None):
        source_path = self.store.path_for(page)
        snapshot = tempfile.TemporaryFile(mode="w+b")
        success = False
        try:
            with source_path.open("rb") as source:
                copy_task = asyncio.create_task(
                    asyncio.to_thread(shutil.copyfileobj, source, snapshot, 1024 * 1024)
                )
                try:
                    await asyncio.shield(copy_task)
                except asyncio.CancelledError:
                    await asyncio.shield(copy_task)
                    raise
            snapshot.seek(0)
            success = True
            return snapshot
        finally:
            if not success:
                snapshot.close()

    async def _send_with_ack(self, sender: str, frame: str) -> None:
        from meshcore import EventType

        async with self._send_lock:
            for attempt in range(2):
                result = await self.mesh.commands.send_msg(sender, frame)
                if result is None or result.type == EventType.ERROR:
                    LOG.warning("Companion refused message, attempt %d", attempt + 1)
                    continue
                payload = result.payload
                code = payload["expected_ack"].hex().lower()
                suggested = float(payload.get("suggested_timeout", 15000)) / 1000 * 1.2
                timeout = max(5.0, min(60.0, suggested))
                if await self._wait_ack(code, timeout):
                    return
                LOG.warning("No ACK for chunk, attempt %d", attempt + 1)
        raise SendFailed("no delivery acknowledgment")

    async def _wait_ack(self, code: str, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if code in self._ack_codes:
                self._ack_codes.remove(code)
                return True
            self._ack_event.clear()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._ack_event.wait(), remaining)
            except asyncio.TimeoutError:
                return False

    async def _send_error(self, sender: str, request_id: str, code: str) -> None:
        try:
            async with self._send_lock:
                await self.mesh.commands.send_msg(sender, error_frame(request_id, code))
        except Exception:
            LOG.exception("Could not send %s to %s", code, sender)


async def configure_companion(mesh: Any, node_name: str) -> None:
    from meshcore import EventType

    result = await mesh.commands.set_name(node_name)
    if result is None or result.type != EventType.OK:
        raise RuntimeError(f"Companion rejected node name {node_name!r}: {result}")
    result = await mesh.commands.send_advert(flood=True)
    if result is None or result.type != EventType.OK:
        raise RuntimeError(f"Companion rejected flood advertisement: {result}")
    key = str(mesh.self_info.get("public_key", "")) if hasattr(mesh, "self_info") else ""
    LOG.info(
        "Configured node %s (key prefix %s) and requested a flood advertisement",
        node_name, key[:12] or "unknown",
    )


async def wait_for_disconnect(mesh: Any) -> Any:
    """Wait indefinitely for a real disconnect, then remove the subscription."""
    from meshcore import EventType

    disconnected = asyncio.get_running_loop().create_future()

    def on_disconnect(event: Any) -> None:
        if not disconnected.done():
            disconnected.set_result(event)

    subscription = mesh.subscribe(EventType.DISCONNECTED, on_disconnect)
    try:
        return await disconnected
    finally:
        mesh.unsubscribe(subscription)


async def run(port: str, baud: int, content: Path, config: ServerConfig) -> None:
    from meshcore import MeshCore

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    store = ContentStore(content)
    while not stop.is_set():
        mesh = None
        server = None
        try:
            mesh = await MeshCore.create_serial(port, baud)
            if mesh is None:
                raise ConnectionError(f"No MeshCore serial companion responded on {port}")
            await configure_companion(mesh, config.node_name)
            server = TeletextServer(mesh, store)
            server.start()
            LOG.info("Listening through %s", port)
            disconnected = asyncio.create_task(wait_for_disconnect(mesh))
            stopped = asyncio.create_task(stop.wait())
            done, pending = await asyncio.wait(
                (disconnected, stopped), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                result = await task
                if task is disconnected:
                    LOG.warning("Companion disconnected: %s", result.payload.get("reason", "unknown"))
        except (OSError, RuntimeError, ConnectionError) as exc:
            LOG.warning("Companion connection or setup failed: %s", exc)
        finally:
            if server is not None:
                try:
                    await server.stop()
                except Exception:
                    LOG.exception("Could not stop server cleanly")
            if mesh is not None:
                try:
                    await mesh.disconnect()
                except Exception:
                    LOG.exception("Could not disconnect companion cleanly")
        if not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="MeshCore Teletext USB server")
    parser.add_argument("--port", required=True, help="USB serial port, e.g. /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--content", type=Path, default=Path("content"))
    parser.add_argument("--config", type=Path, default=Path("config.ini"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    try:
        with exclusive_port(args.port):
            asyncio.run(run(args.port, args.baud, args.content, config))
    except PortInUseError as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    main()
