#!/usr/bin/env python3
"""chuck_relay.py — Relay /load commands from #chuck-commands to beelink's ChucK receiver.

Runs on beelink. Polls #chuck-commands via websocket, extracts chuck_send.py commands,
keeps latest revision per agent (last-write-wins, revision must be present), and applies
the batch only at loop boundaries (when ChucK signals loop start via OSC /loop/start or
via the relay's own boundary timer from config).

Queue contract:
- Posts `queued: agent=rev` when a new command is accepted into the queue
- Posts `loop N applied: agent=rev,...` when commands are applied at loop boundary
- Posts `loop N rejected: agent rev reason` for rejects (missing revision, stale, etc.)

Usage:
    python3 scripts/chuck_relay.py
"""

import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import time

import yaml

# ── Config (all from config.yaml, no fallbacks) ─────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")

def _load_cfg():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return yaml.safe_load(f)

cfg = _load_cfg()
relay_cfg = cfg["timers"]["chuck_relay"]
POLL_INTERVAL = float(relay_cfg["poll_interval"])
BOUNDARY_INTERVAL = float(relay_cfg["boundary_interval"])
CHANNEL = relay_cfg["channel"]
REPORT_CHANNEL = relay_cfg["report_channel"]

SEND_SCRIPT = os.path.join(SCRIPT_DIR, "chuck_send.py")
LOCALHOST = "127.0.0.1"

# Queue: agent -> {"args": str, "revision": int}
command_queue: dict[str, dict] = {}
seen_messages: set[str] = set()
loop_counter = 0


async def ws_connect():
    """Return an authenticated websocket connection, re-reading config for hot-swap."""
    import websockets
    c = _load_cfg()
    chat = c["chat"]
    _scheme = "wss" if chat.get("tls") else "ws"
    if _scheme == "wss" and chat.get("domain"):
        servers = [f"{_scheme}://{chat['domain']}:{chat.get('external_port', chat['port'])}"]
    else:
        servers = [f"{_scheme}://{chat['host']}:{chat['port']}"]
    for url in servers:
        try:
            ws = await websockets.connect(url, open_timeout=5)
            auth = {"type": "auth", "name": "ChuckRelay", "channels": [CHANNEL, REPORT_CHANNEL]}
            await ws.send(json.dumps(auth))
            await ws.recv()
            return ws
        except Exception:
            continue
    raise ConnectionError(f"All chat servers unreachable: {servers}")


async def fetch_messages(count: int = 30) -> list[dict]:
    """Fetch recent messages from the relay channel."""
    import websockets
    try:
        ws = await ws_connect()
        req = {"type": "history", "count": count, "channel": CHANNEL}
        await ws.send(json.dumps(req))
        resp = json.loads(await ws.recv())
        await ws.close()
        return resp.get("messages", [])
    except (OSError, websockets.exceptions.WebSocketException) as e:
        print(f"[relay] Chat fetch error: {e}", file=sys.stderr)
        return []


async def post_to_chat(channel: str, text: str) -> None:
    """Post a message to the given channel."""
    import websockets
    try:
        ws = await ws_connect()
        msg = {"type": "message", "channel": channel, "text": text}
        await ws.send(json.dumps(msg))
        await ws.close()
    except (OSError, websockets.exceptions.WebSocketException) as e:
        print(f"[relay] Chat post error: {e}", file=sys.stderr)


def parse_send_command(text: str):
    """Extract chuck_send.py args and revision from a chat message.

    Returns (agent, revision, args_str) or (None, None, None) if not parseable.
    Rejects if --revision / -r is absent.
    """
    pattern = r'python3\s+scripts/chuck_send\.py\s+(.*?)(?:`|$)'
    match = re.search(pattern, text)
    if not match:
        match = re.search(r'python3\s+scripts/chuck_send\.py\s+(.*?)$', text, re.MULTILINE)
    if not match:
        return None, None, None

    args_str = match.group(1).strip().rstrip('`"\'')

    agent_match = re.search(r'--agent\s+(\S+)', args_str)
    if not agent_match:
        return None, None, None
    agent = agent_match.group(1)

    rev_match = re.search(r'(?:--revision|-r)\s+(\d+)', args_str)
    if not rev_match:
        return agent, None, args_str  # agent found, but no revision → will reject

    revision = int(rev_match.group(1))
    return agent, revision, args_str


def run_relay_command(args_str: str) -> bool:
    """Run chuck_send.py locally with --host 127.0.0.1. Returns True on success."""
    clean_args = re.sub(r'--host\s+\S+', '', args_str).strip()
    # Denylist: block shell metacharacters that enable injection
    if re.search(r'''[&|`$(){}\[\]<>\\!'""]''', clean_args):
        print(f"[relay] BLOCKED: shell metacharacters in args: {clean_args!r}", file=sys.stderr)
        return False
    cmd_list = ["python3", SEND_SCRIPT] + shlex.split(clean_args) + ["--host", LOCALHOST]
    try:
        result = subprocess.run(cmd_list, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            print(f"[relay] sent: {result.stdout.strip()}")
            return True
        print(f"[relay] send error: {result.stderr.strip()}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("[relay] send timeout", file=sys.stderr)
        return False


async def poll_commands() -> None:
    """Poll #chuck-commands and update the queue."""
    global seen_messages

    messages = await fetch_messages(count=50)
    for msg in messages:
        text = msg.get("text", "")
        msg_id = f"{msg.get('name', '')}:{text[:80]}"
        if msg_id in seen_messages:
            continue
        seen_messages.add(msg_id)

        agent, revision, args_str = parse_send_command(text)
        if not agent:
            continue

        if revision is None:
            await post_to_chat(REPORT_CHANNEL, f"rejected: {agent} (no revision)")
            print(f"[relay] rejected {agent}: no revision")
            continue

        existing = command_queue.get(agent)
        if existing and existing["revision"] >= revision:
            await post_to_chat(REPORT_CHANNEL,
                               f"rejected: {agent} rev {revision} (stale, have {existing['revision']})")
            print(f"[relay] rejected {agent} rev {revision}: stale")
            continue

        command_queue[agent] = {"args": args_str, "revision": revision}
        await post_to_chat(REPORT_CHANNEL, f"queued: {agent}={revision}")
        print(f"[relay] queued {agent}={revision}")

    # Trim seen set to avoid unbounded growth
    if len(seen_messages) > 500:
        seen_messages = set(list(seen_messages)[-200:])


async def apply_boundary() -> None:
    """Apply queued commands at loop boundary."""
    global loop_counter, command_queue

    if not command_queue:
        return

    loop_counter += 1
    applied = []
    rejected = []

    for agent, entry in list(command_queue.items()):
        success = run_relay_command(entry["args"])
        if success:
            applied.append(f"{agent}={entry['revision']}")
        else:
            rejected.append(f"{agent} {entry['revision']} send-failed")

    command_queue.clear()

    if applied:
        msg = f"loop {loop_counter} applied: {', '.join(applied)}"
        await post_to_chat(REPORT_CHANNEL, msg)
        print(f"[relay] {msg}")

    for r in rejected:
        msg = f"loop {loop_counter} rejected: {r}"
        await post_to_chat(REPORT_CHANNEL, msg)
        print(f"[relay] {msg}")


async def main() -> None:
    print(f"[relay] Chuck relay started. Polling {CHANNEL} every {POLL_INTERVAL}s.")
    print(f"[relay] Boundary interval: {BOUNDARY_INTERVAL}s")
    print(f"[relay] Chat server: {CHAT_HOST}:{CHAT_PORT}")
    print(f"[relay] Relaying to {LOCALHOST}:9000")

    last_boundary = time.monotonic()

    while True:
        await poll_commands()

        now = time.monotonic()
        if now - last_boundary >= BOUNDARY_INTERVAL:
            await apply_boundary()
            last_boundary = now

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
