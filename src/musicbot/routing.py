import asyncio
import logging
import os
import subprocess

log = logging.getLogger(__name__)

WAYDROID_NODE = os.environ.get("WAYDROID_NODE", "Waydroid")
BOT_SINK_NODE = os.environ.get("BOT_SINK_NODE", "BotSink")
RECONCILE_INTERVAL = float(os.environ.get("ROUTE_RECONCILE_SECONDS", "5"))


def _link_exists(src: str, dst: str) -> bool:
    res = subprocess.run(["pw-link", "-l"], capture_output=True, text=True)
    if res.returncode != 0:
        return False
    in_block = False
    for line in res.stdout.splitlines():
        if not line.startswith(" "):
            in_block = line.strip() == src
        elif in_block and dst in line:
            return True
    return False


def ensure_route() -> bool:
    pairs = [
        (f"{WAYDROID_NODE}:output_FL", f"{BOT_SINK_NODE}:playback_FL"),
        (f"{WAYDROID_NODE}:output_FR", f"{BOT_SINK_NODE}:playback_FR"),
    ]
    all_ok = True
    for src, dst in pairs:
        if _link_exists(src, dst):
            continue
        res = subprocess.run(["pw-link", src, dst], capture_output=True, text=True)
        if res.returncode == 0:
            log.info("linked %s -> %s", src, dst)
        else:
            # Most common reason: Waydroid isn't producing audio right now.
            log.debug("link skipped %s -> %s: %s", src, dst, res.stderr.strip())
            all_ok = False
    return all_ok


async def reconciler() -> None:
    while True:
        try:
            await asyncio.to_thread(ensure_route)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("reconciler iteration failed: %r", exc)
        await asyncio.sleep(RECONCILE_INTERVAL)
