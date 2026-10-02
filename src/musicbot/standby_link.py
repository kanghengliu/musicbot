"""Heartbeat lease to the cloud standby (standby/standby.py) over SSH.

Keeps one long-lived `ssh <host> <relay command>` open and writes a JSON line
to it every HEARTBEAT seconds, and immediately whenever the voice target
changes. The standby treats a lease that stops updating as "local is gone"
and holds the voice channel until we're back. Disabled unless
STANDBY_SSH_HOST is set.
"""

import asyncio
import json
import logging
import os
import signal
import time

log = logging.getLogger(__name__)

HEARTBEAT = 10
DEFAULT_COMMAND = "musicbot-standby/.venv/bin/python musicbot-standby/standby.py relay"

def _ignore_stop_signals() -> None:
    # systemd sends SIGTERM to the whole service cgroup on stop; the handoff
    # needs ssh alive after that, so ssh inherits SIGTERM/SIGINT as ignored
    # (OpenSSH keeps ignored signals ignored). stop() ends it via stdin EOF.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)


# OpenSSH prints these on every connect to servers without PQ key exchange.
_SSH_NOISE = ("post-quantum", "store now, decrypt later", "openssh.com/pq", "server may need to be upgraded")


class StandbyLink:
    def __init__(self, host: str, command: str = DEFAULT_COMMAND):
        self.host = host
        self.command = command
        self.targets: dict[int, int] = {}
        self.state = "alive"
        # Changes whenever we ask the standby to hold the slot for a reconnect.
        self.bounce_id: str | None = None
        self._last_ok = 0.0
        self._wake = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_env(cls) -> "StandbyLink | None":
        host = os.environ.get("STANDBY_SSH_HOST", "").strip()
        if not host:
            return None
        return cls(host, os.environ.get("STANDBY_SSH_COMMAND", DEFAULT_COMMAND))

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="standby-link")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()

    def set_targets(self, targets: dict[int, int]) -> None:
        self.targets = dict(targets)
        self._wake.set()

    @property
    def healthy(self) -> bool:
        proc = self._proc
        return proc is not None and proc.returncode is None and time.monotonic() - self._last_ok < 2 * HEARTBEAT

    async def bounce(self) -> bool:
        """Ask the standby to take the channel now so we can re-handshake."""
        self.bounce_id = f"{time.time():.6f}"
        return await self._send()

    async def handoff(self) -> bool:
        """Tell the standby to take the channel now, ahead of our shutdown."""
        self.state = "handoff"
        return await self._send()

    def _line(self) -> bytes:
        payload = {
            "state": self.state,
            "targets": {str(gid): str(cid) for gid, cid in self.targets.items()},
            "bounce": self.bounce_id,
            "sent_at": time.time(),
        }
        return (json.dumps(payload) + "\n").encode()

    async def _send(self) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(self._line())
            await asyncio.wait_for(proc.stdin.drain(), timeout=5)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("standby link: write failed: %r", exc)
            return False
        self._last_ok = time.monotonic()
        return True

    async def _run(self) -> None:
        failures = 0
        while True:
            started = time.monotonic()
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    "ssh", "-T",
                    "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=15",
                    "-o", "ServerAliveInterval=10",
                    "-o", "ServerAliveCountMax=3",
                    self.host, self.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    preexec_fn=_ignore_stop_signals,
                )
            except OSError as exc:
                log.warning("standby link: can't start ssh: %r", exc)
            else:
                log.info("standby link: ssh to %s started", self.host)
                while self._proc.returncode is None:
                    if not await self._send():
                        break
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), HEARTBEAT)
                    except asyncio.TimeoutError:
                        pass
                rc = await self._proc.wait()
                err = b""
                if self._proc.stderr is not None:
                    err = await self._proc.stderr.read()
                lines = [
                    ln for ln in err.decode(errors="replace").splitlines()
                    if ln.strip() and not any(n in ln for n in _SSH_NOISE)
                ]
                log.warning("standby link: ssh exited %s%s", rc, f": {lines[-1]}" if lines else "")

            failures = 0 if time.monotonic() - started > 60 else failures + 1
            delay = min(60, 5 * 2 ** max(failures - 1, 0))
            await asyncio.sleep(delay)
