# Collection recovery fix — 2026-10-28

Read-only public collection remains enabled; no order, signing or funds adapter was added.

## Causes and changes

- websockets 15.0.1 can receive `connection_lost` during proxy TLS setup before `connection_made` initializes `recv_messages`. The original exception then prevents normal cleanup. A small `ClientConnection` subclass initializes an empty assembler at construction. Normal connection setup replaces it with the transport-backed assembler. The original exception and upstream cleanup still run: failures are not swallowed, TLS verification and system proxy selection are unchanged. All three public WS consumers use this wrapper. Dependency remains pinned to15.0.1; review/remove this compatibility layer on upgrade.
- Actual stored sports messages contain `ended=true`, `live=false`, `status=closed`, `period=VFT`. The previous adapter recognized only Final/F/OT. It now recognizes this explicit ended+closed combination only with exact game ID, NBA league and home/away identity. A closed CLOB market, settlement event, score or period alone does not start the timer. Conflicting live=true is rejected. Quote authorization remains false.
- Existing PilotWindow freshness check, first-observed-end latch, persisted end timestamp, 30-minute tail and hard-limit fallback remain unchanged. No historical timestamps or databases were rewritten.

## Validation

- Full suite:393 tests passed in52.246s. `STORAGE_MAINTENANCE_FAILED: injected` in the log is an expected failure-injection test, not an unreported suite failure.
- New tests cover disconnect before connection_made, normal local WebSocket connection/reconnection, ended+closed tail boundary, duplicate end not extending tail, stale updates, wrong game/team/league, false/string ended, contradictory live flag and existing Final/F/OT.
- Read-only historical validation recognized end messages in12 of13 examined sessions from Oct27/28. The remaining session had no matching stored sports end; this does not create evidence where none exists. `hypothetical_tail_stop_ms` is an offline calculation, not the actual stop time.
- Public sports WS smoke: first attempt timed out; second connected and received a message. This fix removes the cleanup AttributeError, not network outages. Missing events cannot be recovered by reconnecting; existing snapshots and gap markers remain necessary.

## Deployment

The active Oct28 worker is deliberately left running with its already imported code. It will not gain the fix mid-game. The idle Oct29 worker is reloaded after confirming all its markets are SCHEDULED; supervisor and active writer are not restarted. Future new processes load the fix. No in-place dependency upgrade or site-packages edit.

## Remaining limits

No promise of zero disconnections. Gamma availability can still fail; the sports end path now works independently when its exact matching message arrives. If neither supplies trustworthy end evidence, the hard-limit result must remain NOT_COMPLETE. Previously recorded gaps and late/hard-limit stops remain historical facts.

Upstream connection extension point: https://websockets.readthedocs.io/en/15.0/reference/asyncio/client.html
Proxy behavior: https://websockets.readthedocs.io/en/15.0/topics/proxies.html
