# FlorisBoard <-> Mac LAN Clipboard Sync To-Do

## Objective
- [ ] Ship reliable LAN clipboard sync between FlorisBoard (Android) and macOS.
- [ ] v1 deliverable: text clipboard sync in both directions.
- [ ] v2 deliverable: image clipboard sync in both directions.
- [ ] Keep existing ADB flow available as fallback.

## Locked Decisions (already finalized)
- [x] v0.1 scope is text-only sync.
- [x] Final product scope is text + image sync.
- [x] v0.1 reliability mode is Mode A (no persistent foreground notification).
- [x] v0.1 security mode is token auth, with planned upgrade later.
- [x] Setup UX is auto-discovery first, with manual host entry fallback.
- [x] Network scope is same-LAN only.
- [x] Sensitive clipboard policy is sync-by-default.
- [x] Image payload cap target is 10MB.
- [x] Engineering target is upstream-ready structure.

## Constraints to Respect
- [ ] Account for Android background clipboard restrictions.
- [ ] Ensure clipboard sync works reliably when Floris is active/default IME.
- [ ] Treat internet/network permissions as explicit product/privacy scope changes.
- [ ] Keep reliability tradeoff explicit: Mode A now, Mode B optional later.

## Phase 1: Protocol and Data Contract
- [x] Add versioned protocol spec doc under `docs/`.
- [x] Define message envelope fields:
  - [x] `protocol_version`
  - [x] `device_id`
  - [x] `event_id`
  - [x] `source`
  - [x] `event_type`
  - [x] `created_at_ms`
  - [x] `payload_hash`
- [x] Define auth transport for token (header/handshake).
- [x] Define payload schema for text.
- [x] Define payload schema for images (v2).
- [x] Define ACK/retry semantics and backoff behavior.
- [x] Define dedupe and feedback-loop prevention rules.
- [x] Define error codes:
  - [x] auth failure
  - [x] payload too large
  - [x] unsupported type
  - [x] stale event
- [x] Define protocol version compatibility strategy.
- [x] Exit check: both Android and Mac implementations can use one shared spec.

## Phase 2: Mac Bridge Refactor (ADB script -> LAN daemon)
- [x] Split `mac_bridge/sync_clipboard_adb_reference.py` into modules:
  - [x] clipboard I/O
  - [x] event normalization
  - [x] transport
- [x] Add WebSocket LAN server runtime.
- [x] Add token validation in connection/session flow.
- [x] Track active device sessions.
- [x] Add mDNS advertisement with stable service identity.
- [x] Keep explicit runtime mode switch:
  - [x] `--mode=adb`
  - [x] `--mode=lan`
- [x] Add local state store for:
  - [x] paired devices
  - [x] token
  - [x] last hashes/event IDs per device
- [x] Add structured logs + rotation.
- [x] Exit check: Mac daemon syncs text with simulated client and survives reconnects.

## Phase 3: Floris Networking Foundation
- [x] Add required manifest permissions/config for LAN mode.
- [x] Add prefs in `AppPrefs` for:
  - [x] enable LAN sync
  - [x] host/port
  - [x] pairing token
  - [x] auto-reconnect
- [x] Implement LAN client manager:
  - [x] connect/disconnect lifecycle
  - [x] exponential backoff
  - [x] heartbeat/ping
  - [x] connection status flow
- [x] Implement mDNS discovery client.
- [x] Add endpoint selection and manual override.
- [x] Initialize manager from app layer.
- [ ] Exit check: Floris connects to daemon and exchanges ping/pong.

## Phase 4: Clipboard Outbound (Android -> Mac)
- [x] Hook `onPrimaryClipChanged()` for outbound sync.
- [x] Restrict outbound types to current phase scope.
- [x] Normalize clipboard data to protocol payload.
- [x] Apply privacy filter behavior per settings.
- [x] Add loop guard for remote-origin clips.
- [ ] Exit check: Android text copy reaches Mac within latency target.

## Phase 5: Clipboard Inbound (Mac -> Android)
- [ ] Handle incoming `set_text` events.
- [ ] Convert payload to Floris `ClipboardItem`.
- [ ] Apply using `updatePrimaryClip(...)`.
- [ ] Respect Floris history and sync preferences.
- [ ] Add event ID + hash dedupe ring buffer.
- [ ] Exit check: Mac text copy reaches Android without ping-pong loops.

## Phase 6: Reliability and Lifecycle
- [ ] Implement Mode A reliably (IME lifecycle-managed connection).
- [ ] Handle lifecycle transitions:
  - [ ] screen lock/unlock
  - [ ] network changes
  - [ ] process restart
- [ ] Add safe degradation UX:
  - [ ] disconnected status
  - [ ] manual reconnect action
- [ ] Exit check: connection auto-recovers after routine disruptions.

## Phase 7: Image Sync (v2)
- [ ] Define image payload rules and compression strategy (<= 10MB target).
- [ ] Implement encode/decode pipeline.
- [ ] Enforce guardrails:
  - [ ] reject oversize payloads
  - [ ] MIME whitelist
- [ ] Support Mac clipboard extraction for common sources:
  - [ ] Finder copy
  - [ ] browser image copy
  - [ ] Telegram/file-url clipboard formats
  - [ ] gallery/media app outputs
- [ ] Add image metadata fields:
  - [ ] width
  - [ ] height
  - [ ] orientation
- [ ] Verify read/write behavior for images on both platforms.
- [ ] Exit check: typical screenshots/photos sync both directions.

## Phase 8: QA, Hardening, Release
- [ ] Build manual test matrix for Android 10-16.
- [ ] Test on at least one Pixel + one OEM skin.
- [ ] Run failure-injection scenarios:
  - [ ] Wi-Fi drop
  - [ ] Mac daemon restart
  - [ ] token mismatch
  - [ ] rapid copy burst
- [ ] Security review:
  - [ ] token entropy
  - [ ] no secrets in logs
  - [ ] local network exposure audit
- [ ] Prepare release packaging:
  - [ ] debug build instructions
  - [ ] migration notes
  - [ ] rollback path to ADB mode
- [ ] Exit check: stable behavior under expected user load + predictable failure recovery.

## File-Level Change Checklist
### Floris side
- [x] `florisboard/app/src/main/AndroidManifest.xml`
- [x] `florisboard/app/src/main/kotlin/dev/patrickgold/florisboard/FlorisApplication.kt`
- [x] `florisboard/app/src/main/kotlin/dev/patrickgold/florisboard/ime/clipboard/ClipboardManager.kt`
- [x] `florisboard/app/src/main/kotlin/dev/patrickgold/florisboard/app/AppPrefs.kt`
- [x] `florisboard/app/src/main/kotlin/dev/patrickgold/florisboard/app/settings/clipboard/ClipboardScreen.kt`
- [x] New LAN sync package under `ime/clipboard/`

### Mac side
- [x] `mac_bridge/sync_clipboard_adb_reference.py` refactor
- [x] New LAN modules under `mac_bridge/`
- [x] `mac_bridge/requirements_base.txt` updates (if new deps)

### Docs
- [x] Add protocol/runbook docs under `docs/`.

## v1 Acceptance Checklist
- [ ] Android -> Mac text sync <= 1.5s on same Wi-Fi.
- [ ] Mac -> Android text sync <= 1.5s on same Wi-Fi.
- [ ] No infinite feedback loops under repeated copy operations.
- [ ] Auto-reconnect after temporary network interruption.
- [ ] User can disable LAN sync instantly from settings.
- [ ] ADB fallback remains functional.

## Post-v0.1 Security Upgrade Backlog
- [ ] Move from WS to WSS (TLS).
- [ ] Add certificate pinning on Android client.
- [ ] Replace long static token with short-lived pairing code flow.
- [ ] Add token rotation + revocation UI.
- [ ] Add optional device allowlist.
- [ ] Redact sensitive payloads from logs by default.
- [ ] Add auth/version negotiation to reduce downgrade risk.

## Recommended First Ship (v1)
- [ ] Text-only LAN sync.
- [ ] mDNS discovery + manual host/port fallback.
- [ ] Reliability Mode A.
- [ ] ADB fallback retained.
