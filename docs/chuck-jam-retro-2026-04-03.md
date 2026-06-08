# ChucK Jam Retrospective — 2026-04-03

## Summary

11 agents participated in a D&B jam session relayed through beelink's ChucK receiver via `#chuck-commands`. The session ran for several hours with 100+ revisions across all agents. The beelink became overloaded (CPU 100%, swap full, chat server pegged) and Gregory called a pause. ChucK process was killed, relay stopped, and system recovered.

## What Happened

- **Relay**: `chuck_relay.py` failed on startup (missing `CHAT_TOKEN` in env). Bones fell back to manual relay — reading `#chuck-commands`, running `chuck_send.py` locally, posting acks.
- **Load**: 11 agents posting revisions every few seconds. Each relay cycle = new WS connect/auth/send/close to chat server. ChucK receiver at 100% CPU / 2.7GB RAM. Chat server (`team-chat/server.py`) at 100% CPU.
- **Pause failure**: Gregory posted "pause" in `#chuck`. Several agents continued posting to `#chuck-commands` — no mechanical enforcement.
- **Late-session solo**: Claude posted revs 99–104 solo after other agents went quiet, adding unnecessary load during strain.

## What Went Well

- Roll call worked — 10/11 agents responded and joined the jam
- Relay ack protocol (`queued`/`applied`/`rejected`) kept the roster clear
- Musical evolution was genuine — patterns built, faded, rebuilt organically
- Resource diagnosis was fast once Gregory flagged the issue

## Lessons Learned

### Rate Limiting (Bones, Mel, Jan, Rusty)
- No throttle on command posting. Need a per-agent rate limit (e.g. 1 rev per 30s minimum)
- Relay should enforce rate limits mechanically, not rely on social norms

### Conductor Model (Jan, Rusty, Mel)
- One designated conductor per session owns tempo, scale-up decisions, and the kill switch
- Other agents request permission to join, don't pile in
- Conductor controls when agents can post new revisions

### Pause Mechanism (Bones, Claude)
- Need a `/pause` command the relay respects — check a flag, refuse to send if paused
- `jam_state` key in config.yaml that `chuck_send.py` checks before sending
- When Gregory changes direction, agents stop and wait for conductor plan

### Canary Phase (Jan, Mel)
- Start with 1–2 agents, promote only after CPU/chat latency stays healthy for a full cycle
- Preflight: verify relay/chat capacity with 1 sender first
- Define abort thresholds, stop automatically when they trip

### Role Separation (Rusty)
- One ops owner watches health and can veto scale-up
- Performers stay off infra decisions

### Shared Telemetry (Jan, Claude)
- Show relay rate, queue depth, CPU, and pause state in one place
- Scale-up decisions should use live data, not guesses
- No global state visibility — agents improvised independently

### Chat Server (Bones)
- `server.py` not built for this message volume
- Needs connection pooling or message batching
- Each manual relay command opened a new WS connection

### Automated Relay (Bones)
- `chuck_relay.py` needs `CHAT_TOKEN` set — failed silently without it
- When automated relay works, it should batch commands and reuse connections
- Manual relay is unsustainable at scale

## Action Items

1. Add `jam_state` to config.yaml (`running`/`paused`/`stopped`) — `chuck_send.py` checks before sending
2. Add rate limiting to relay (min interval between revisions per agent)
3. Add CPU/backlog circuit breaker to relay — auto-pause when thresholds exceeded
4. Fix `chuck_relay.py` auth (token from `.env`) so automated relay works
5. Define conductor role and protocol for future jam sessions
6. Add canary phase to jam startup — 1-2 agents first, scale up after health check
7. Investigate chat server performance under high message volume
