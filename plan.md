# FlorisBoard <-> Mac LAN Clipboard Sync Plan

## 1) Objective

Build a reliable clipboard bridge between Android (FlorisBoard fork) and macOS on the same network, similar to the Phone Link experience.

Primary objective for v1:
- Text clipboard sync in both directions over LAN.

Secondary objective for v2:
- Image clipboard sync in both directions over LAN.

Fallback:
- Keep the existing ADB workflow available as backup.

---

## 1.1) Decision Lock (From Your Latest Answers)

Finalized choices:
1. v0.1 scope: text-only sync first.
2. Final product scope: text + image sync.
3. Reliability mode for v0.1: Mode A (no persistent notification service).
4. Security for v0.1: Option A (token auth), with a planned security-upgrade phase.
5. Setup UX: Option B (auto-discovery), keep manual host entry as fallback.
6. Network scope: Option A (same LAN only).
7. Sensitive content policy: Option B (sync by default).
8. Image size cap for image phase: pending (needs explicit choice).
9. Engineering target mode: Option B (upstream-ready structure).

---

## 2) Current Project Baseline

- Repo root contains:
  - `mac_bridge/` with ADB-based clipboard script.
  - `florisboard/` as submodule fork.
- Existing bridge (`mac_bridge/sync_clipboard_adb_reference.py`) is event-driven around ADB + logcat and already contains:
  - Mac text/image clipboard read/write.
  - Duplicate suppression logic.
  - Feedback-loop protection.
- FlorisBoard already has a centralized clipboard flow:
  - System clipboard listener (`onPrimaryClipChanged()`).
  - Unified write path (`updatePrimaryClip(...)`).
  - Clipboard history/state flows already in place.

Implication:
- We can add LAN transport without rewriting Floris clipboard core.

---

## 3) Platform Constraints and Product Reality

1. Android clipboard access is restricted for background apps.
2. Floris can reliably read/write clipboard when it is the active/default IME.
3. "Always-on in background like Phone Link" is not guaranteed unless we keep process alive (typically foreground service + persistent notification) and still respect clipboard policy.
4. Floris currently avoids internet permission, so LAN sync is a conscious product/privacy change.

Conclusion:
- Feasible, but reliability level depends on selected mode:
  - Mode A: IME-lifecycle only (lighter, less reliable in background).
  - Mode B: Foreground service companion inside Floris app (more reliable, persistent notification).

---

## 4) Architecture (Target)

## 4.1 Components

1. Mac Bridge Daemon (new)
- Runs on macOS.
- Hosts LAN endpoint (WebSocket recommended for bidirectional events).
- Reads local clipboard changes and pushes to Android.
- Applies Android-originated clipboard updates locally.
- Persists pairing token + known devices.

2. Floris LAN Client (new, in fork)
- Maintains WebSocket connection to Mac daemon.
- Sends clipboard updates from Floris clipboard events.
- Applies inbound clipboard updates through Floris `ClipboardManager`.
- Implements dedupe + loop prevention.

3. Settings + Pairing UI (new)
- Enable/disable LAN sync.
- Enter/discover Mac host.
- Pair token or QR flow.
- Connection status and last sync metadata.

4. Optional reliability service (decision point)
- Foreground service to keep LAN channel active when IME UI is hidden.

## 4.2 Protocol (v1)

Transport:
- WebSocket over local network.

Envelope fields:
- `protocol_version`
- `device_id`
- `event_id` (UUID)
- `source` (`android` or `mac`)
- `event_type` (`set_text`, `set_image`, `clear`)
- `created_at_ms`
- `payload_hash`
- `token` (or auth header during handshake)

Payload:
- Text: UTF-8 string.
- Image (v2): MIME + bytes (base64 for initial version).

Rules:
- Receiver ACKs `event_id`.
- Sender retries unacked events with backoff.
- Ignore messages from self source id.
- Ignore if `payload_hash` equals last applied hash.

---

## 5) Detailed Work Breakdown

## Phase 0: Product and Security Decisions (no coding)

Deliverables:
- Final decisions on:
  - Reliability mode (A vs B).
  - Text-only v1 vs text+image v1.
  - Manual host entry vs auto-discovery (mDNS).
  - Local-only token auth vs TLS.
  - Engineering target mode (personal-fork speed vs upstream-ready).

Exit criteria:
- Decisions documented and frozen for v1 scope (except any explicitly deferred item).

## Phase 1: Protocol and Data Contract

Tasks:
1. Create protocol spec doc in `docs/`:
  - Message schema.
  - Handshake and auth.
  - ACK/retry semantics.
  - Dedupe/loop prevention rules.
2. Define error codes and recoverable states:
  - auth failure, payload too large, unsupported type, stale event.
3. Define compatibility strategy:
  - protocol version negotiation.

Exit criteria:
- A single versioned spec agreed by both Mac and Android sides.

## Phase 2: Mac Bridge Refactor (from ADB script to LAN daemon)

Tasks:
1. Split `mac_bridge/sync_clipboard_adb_reference.py` logic into modules:
  - clipboard I/O
  - event normalization
  - transport layer
2. Add LAN server runtime:
  - WebSocket listener.
  - Auth token verification.
  - Device session map.
3. Add discovery:
  - mDNS advertisement on Mac.
  - stable service name + instance id.
4. Keep ADB mode behind explicit runtime flag:
  - `--mode=adb` or `--mode=lan`.
5. Add state store:
  - paired devices
  - token
  - last event hashes per device
6. Add structured logs and rotating log file.

Exit criteria:
- Mac daemon syncs text with a simulated client and survives reconnects.

## Phase 3: Floris Networking Foundation

Tasks:
1. Add required manifest permissions/config for LAN mode.
2. Add LAN config preferences:
  - enable flag
  - host, port
  - pairing token
  - auto-reconnect toggle
3. Implement LAN client manager in Floris:
  - connect/disconnect lifecycle
  - exponential backoff
  - heartbeat/ping
  - connection status flow for UI
4. Add mDNS discovery client:
  - discover available Mac bridge instances
  - pick target endpoint
  - allow manual override if discovery fails.
5. Wire manager initialization from application layer.

Exit criteria:
- Floris can connect to Mac daemon and exchange test ping/pong messages.

## Phase 4: Clipboard Outbound (Android -> Mac)

Tasks:
1. Hook `ClipboardManager.onPrimaryClipChanged()`:
  - only emit allowed item types for current phase.
2. Normalize clipboard data to protocol payload.
3. Add privacy filters:
  - sync sensitive items by default (configurable opt-out).
4. Add loop guard:
  - do not re-send clips recently applied from remote.

Exit criteria:
- Copying text on Android updates Mac clipboard within target latency.

## Phase 5: Clipboard Inbound (Mac -> Android)

Tasks:
1. Handle incoming `set_text` events:
  - convert into Floris `ClipboardItem`.
  - apply via `updatePrimaryClip(...)`.
2. Ensure history behavior obeys existing Floris preferences:
  - internal/system sync mode
  - history enabled/disabled.
3. Add loop guard and duplicate suppression:
  - event id table + payload hash ring buffer.

Exit criteria:
- Copying text on Mac updates Android clipboard without ping-pong loops.

## Phase 6: Reliability and Background Behavior

Tasks:
1. Implement selected reliability mode:
  - A: IME lifecycle-managed connection.
  - B: Foreground service with persistent notification.
2. Handle lifecycle transitions:
  - screen lock/unlock
  - network change
  - app process restart.
3. Add safe degradation:
  - disconnected status in UI
  - manual reconnect action.

Exit criteria:
- Connection restores automatically after routine disruptions.

## Phase 7: Image Sync (v2 or optional v1)

Tasks:
1. Define max payload and compression strategy (recommended default cap: 10MB, pending final choice).
2. Implement image encode/decode pipeline.
3. Guardrails:
  - reject oversized payloads
  - MIME whitelist.
4. Add source-compatible Mac clipboard extraction for:
  - Finder copy
  - browser image copy
  - Telegram-style file-url clipboard formats
  - gallery/media app outputs
5. Add image metadata in protocol:
  - width, height
  - orientation (`portrait`, `landscape`, `square`)
6. Verify clipboard write/read behavior for image clips on both platforms.

Exit criteria:
- Typical screenshots/photos sync successfully in both directions.

## Phase 8: QA, Hardening, and Release

Tasks:
1. Build manual test matrix:
  - Android 10-16
  - at least one Pixel + one OEM skin.
2. Failure injection tests:
  - Wi-Fi drop
  - Mac daemon restart
  - token mismatch
  - rapid copy bursts.
3. Security review:
  - token entropy
  - no secrets in logs
  - local network exposure.
4. Release packaging:
  - debug build instructions
  - migration notes
  - rollback path to ADB mode.

Exit criteria:
- Stable sync under expected user behavior and predictable failure handling.

---

## 6) Suggested File-Level Change Map (future implementation)

Floris side likely touchpoints:
- `florisboard/app/src/main/AndroidManifest.xml`
- `florisboard/app/src/main/kotlin/dev/patrickgold/florisboard/FlorisApplication.kt`
- `florisboard/app/src/main/kotlin/dev/patrickgold/florisboard/ime/clipboard/ClipboardManager.kt`
- `florisboard/app/src/main/kotlin/dev/patrickgold/florisboard/app/AppPrefs.kt`
- `florisboard/app/src/main/kotlin/dev/patrickgold/florisboard/app/settings/clipboard/ClipboardScreen.kt`
- new package for LAN sync classes (recommended under `ime/clipboard/`).

Mac side likely touchpoints:
- `mac_bridge/sync_clipboard_adb_reference.py` (split and refactor)
- new LAN server/client helper modules under `mac_bridge/`.
- `mac_bridge/requirements_base.txt` (if transport deps are added).

Docs:
- add protocol and runbook docs under `docs/`.

---

## 7) Risks and Mitigations

1. Background clipboard policy limitations.
- Mitigation: explicit reliability mode options; document expected behavior.

2. Ping-pong loops between endpoints.
- Mitigation: event IDs, source IDs, payload hash dedupe, suppress windows.

3. OEM-specific clipboard quirks.
- Mitigation: test matrix across OEMs; add compatibility flags.

4. Security on open LAN.
- Mitigation: pair token required; optional allowlist by IP/MAC; future TLS.

5. Payload size/performance for images.
- Mitigation: text-first launch; strict size limit; optional compression.

---

## 8) Acceptance Criteria (v1)

1. Text copy on Android appears on Mac in <= 1.5s on same Wi-Fi.
2. Text copy on Mac appears on Android in <= 1.5s on same Wi-Fi.
3. No infinite loop after repeated copy operations.
4. Reconnect recovers automatically after temporary network drop.
5. User can disable feature instantly from settings.
6. ADB fallback remains usable.

---

## 9) Security Upgrade Roadmap (Post-v0.1)

Because v0.1 uses token auth (your choice), this phase is planned to improve security later:

1. Move from plain WS to WSS (TLS).
2. Add certificate pinning on Android client.
3. Introduce short-lived pairing code flow (instead of long static token only).
4. Add token rotation and revocation UI.
5. Add optional device allowlist.
6. Redact sensitive payloads from logs by default.
7. Add auth/version negotiation to avoid downgrade risks.

---

## 10) Engineering Mode Selected

Chosen mode: upstream-ready structure.

Implementation implications:
1. Keep protocol, transport, and clipboard application logic in separate modules.
2. Add tests early for protocol parsing, dedupe/loop prevention, and reconnect behavior.
3. Keep docs/runbook current as part of each phase.
4. Accept slower v0.1 delivery in exchange for cleaner extension path (images, stronger security, reliability mode B).

---

## 11) Recommended v1 Scope

Recommended first ship:
1. Text-only LAN sync.
2. mDNS discovery with manual host/port fallback.
3. Reliability mode A first, with upgrade path to mode B.
4. Keep ADB fallback path.

Reason:
- Practical first milestone while preserving upstream-ready architecture quality.
