"""
Scanner sidecar — subprocess manager for the Phase A Rust scanner binary.

Launches `polymm-scanner` as a child process and exchanges NDJSON
messages over stdin/stdout. The sidecar is opt-in via
POLYMM_SCANNER_BINARY env var; if unset, callers should fall back to
the existing Python scanner.

See rust/PHASE_A_PLAN.md section 4 (IPC) for the contract.
"""
import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)


# Default watchdog — if a tick takes this long with no tick_complete,
# something is wrong. Matches the plan's "60s per-tick watchdog".
TICK_TIMEOUT_SECONDS = 60.0


class ScannerSidecarError(Exception):
    """Raised when the sidecar binary cannot be started or has crashed."""


@dataclass
class TickResult:
    tick_id: int
    duration_ms: int
    matched_count: int
    opportunity_count: int
    opportunities: list[dict]


class ScannerSidecar:
    """Manages a long-running polymm-scanner subprocess.

    Usage:
        sidecar = ScannerSidecar.from_env()
        if sidecar is not None:
            await sidecar.start()
            try:
                result = await sidecar.run_tick(tick_id=1, skip_tokens={...})
            finally:
                await sidecar.stop()
    """

    def __init__(self, binary_path: str):
        self.binary_path = binary_path
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._start_lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> Optional["ScannerSidecar"]:
        """Return a sidecar if POLYMM_SCANNER_BINARY is set and points to
        an executable; otherwise None. Callers fall back to the Python
        scanner on None."""
        path = os.environ.get("POLYMM_SCANNER_BINARY")
        if not path:
            return None
        resolved = shutil.which(path) or (path if os.path.isfile(path) else None)
        if not resolved or not os.access(resolved, os.X_OK):
            logger.warning("POLYMM_SCANNER_BINARY=%s not executable; disabling sidecar", path)
            return None
        return cls(resolved)

    async def start(self) -> None:
        async with self._start_lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    self.binary_path,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as e:
                raise ScannerSidecarError(f"failed to launch {self.binary_path}: {e}") from e

            self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def stop(self, timeout: float = 2.0) -> None:
        """Send shutdown, wait for clean exit, then kill if it lingers."""
        proc = self._proc
        if proc is None:
            return
        self._proc = None

        if proc.returncode is None:
            try:
                await self._send({"type": "shutdown"}, proc=proc)
            except (BrokenPipeError, ConnectionResetError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("scanner did not exit within %ss; terminating", timeout)
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.error("scanner still alive; sending SIGKILL")
                    proc.kill()
                    await proc.wait()

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._stderr_task = None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def run_tick(
        self,
        tick_id: int,
        skip_tokens: Optional[set[str]] = None,
        live_match_ids: Optional[set[str]] = None,
        timeout: float = TICK_TIMEOUT_SECONDS,
    ) -> TickResult:
        """Send a scan_request, collect events until tick_complete, return
        the result.

        Raises ScannerSidecarError if the sidecar isn't alive, or
        asyncio.TimeoutError if the tick exceeds `timeout`.
        """
        proc = self._proc
        if proc is None or proc.returncode is not None:
            raise ScannerSidecarError("scanner not running")

        msg = {
            "type": "scan_request",
            "tick_id": tick_id,
            "skip_tokens": sorted(skip_tokens or ()),
            "live_match_ids": sorted(live_match_ids or ()),
        }
        await self._send(msg, proc=proc)

        return await asyncio.wait_for(
            self._collect_tick(proc, tick_id), timeout=timeout,
        )

    async def _collect_tick(
        self, proc: asyncio.subprocess.Process, tick_id: int,
    ) -> TickResult:
        opportunities: list[dict] = []
        async for event in self._read_events(proc):
            etype = event.get("type")
            if etype == "opportunity":
                if event.get("tick_id") == tick_id:
                    opportunities.append(event)
            elif etype == "tick_complete":
                if event.get("tick_id") != tick_id:
                    logger.warning("tick_complete tick_id mismatch: %r (expected %d)", event, tick_id)
                    continue
                return TickResult(
                    tick_id=tick_id,
                    duration_ms=int(event.get("duration_ms", 0)),
                    matched_count=int(event.get("matched_count", 0)),
                    opportunity_count=int(event.get("opportunity_count", 0)),
                    opportunities=opportunities,
                )
            elif etype == "error":
                logger.error("scanner error event: %r", event)
                # Keep reading — an error may still be followed by tick_complete.
            else:
                logger.warning("unknown event type: %r", event)

        # stdout EOF before tick_complete — sidecar died mid-tick.
        raise ScannerSidecarError(f"scanner stdout closed mid-tick {tick_id}")

    async def _send(self, obj: dict, proc: asyncio.subprocess.Process) -> None:
        if proc.stdin is None or proc.stdin.is_closing():
            raise ScannerSidecarError("scanner stdin closed")
        line = (json.dumps(obj) + "\n").encode()
        proc.stdin.write(line)
        await proc.stdin.drain()

    async def _read_events(
        self, proc: asyncio.subprocess.Process
    ) -> AsyncIterator[dict]:
        if proc.stdout is None:
            return
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("malformed stdout line: %r (%s)", line, e)

    async def _drain_stderr(self) -> None:
        """Forward scanner stderr to Python logger. Each line is a JSON
        log event from the Rust side; parse and route by level."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            try:
                entry = json.loads(text)
                level = entry.get("level", "info").lower()
                msg = entry.get("msg", "")
                log_level = {
                    "debug": logging.DEBUG,
                    "info": logging.INFO,
                    "warn": logging.WARNING,
                    "warning": logging.WARNING,
                    "error": logging.ERROR,
                }.get(level, logging.INFO)
                logger.log(log_level, "[scanner] %s %s", msg, {k: v for k, v in entry.items() if k not in ("msg", "level", "ts")})
            except json.JSONDecodeError:
                logger.info("[scanner] %s", text)
