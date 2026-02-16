# LAN Clipboard Sync Protocol v1.0

Status: Draft accepted for Phase 1 implementation.

This document is the shared data contract for FlorisBoard (Android) and macOS bridge LAN clipboard sync.

## 1. Scope

- Same-LAN transport only.
- v1 product deliverable: text clipboard sync in both directions.
- v2 reserved contract: image clipboard sync in both directions.
- Existing ADB flow remains a fallback and is out of scope for this protocol.

## 2. Transport and Session Handshake

- Transport: WebSocket over LAN (`ws://` for v1 mode).
- Suggested path: `/v1/clipboard`.
- Auth is mandatory during handshake via header:
  - `Authorization: Bearer <pairing_token>`
- The client must also send:
  - `X-Clipboard-Protocol-Version: 1.0`
  - `X-Clipboard-Device-Id: <device_id>`
  - `X-Clipboard-Source: android|mac`
- If auth fails, server rejects handshake (`401`) or closes the upgraded socket with close code `4401` and `AUTH_FAILURE`.

## 3. Message Envelope

Every message on the socket is one JSON object with this envelope:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `protocol_version` | string | yes | `major.minor`, currently `1.0` |
| `device_id` | string | yes | Stable per app install; 3-128 chars; `[A-Za-z0-9._:-]+` |
| `event_id` | string | yes | Unique per event; UUIDv4 or ULID recommended |
| `source` | string | yes | `android` or `mac` |
| `event_type` | string | yes | `set_text`, `set_image`, `ack`, `error`, `ping`, `pong` |
| `created_at_ms` | integer | yes | Unix epoch in milliseconds (UTC) |
| `payload_hash` | string | yes | Lowercase hex SHA-256 of canonical payload JSON |
| `payload` | object | yes | Event-specific payload |

Canonical payload JSON means:

- UTF-8 encoding.
- Object keys sorted lexicographically at all levels.
- No insignificant whitespace.
- Integers serialized as base-10 numbers.

## 4. Event Payload Schemas

Machine-readable schemas:

- `docs/protocol/v1/message.schema.json`
- `docs/protocol/v1/payload.set_text.schema.json`
- `docs/protocol/v1/payload.set_image.schema.json`

### 4.1 `set_text` payload (v1 required)

```json
{
  "mime_type": "text/plain",
  "text": "copied text",
  "is_sensitive": false
}
```

Rules:

- `mime_type` must be `text/plain`.
- `text` is UTF-8 text content (empty string allowed, though normal sync should skip empties unless intentional).
- `is_sensitive` defaults to `false`.

### 4.2 `set_image` payload (v2 reserved)

```json
{
  "mime_type": "image/png",
  "byte_size": 93210,
  "data_base64": "iVBORw0K...",
  "width": 1080,
  "height": 2400,
  "orientation": 0
}
```

Rules:

- `mime_type` allowed: `image/png`, `image/jpeg`, `image/webp`.
- `byte_size` max: `10,485,760` bytes (10 MB).
- `data_base64` must be RFC 4648 base64 (standard alphabet with valid padding).
- Receiver must decode `data_base64` and verify decoded byte length equals `byte_size`.
- Receiver must reject payloads where decoded byte length exceeds `10,485,760` bytes (10 MB), regardless of declared `byte_size`.
- `width` and `height` are positive integers.
- `orientation` allowed: `0`, `90`, `180`, `270`.

### 4.3 `ack` payload

```json
{
  "acked_event_id": "01JABW8P73QZZ0C3M4YH3DPVK2",
  "status": "accepted"
}
```

`status` enum:

- `accepted`: event applied.
- `duplicate`: event already seen and ignored.
- `rejected`: event not applied; `error_code` required.

### 4.4 `error` payload

```json
{
  "code": "UNSUPPORTED_TYPE",
  "message": "set_image is disabled in v1 mode",
  "retryable": false
}
```

## 5. ACK, Retry, and Backoff

ACK required for:

- `set_text`
- `set_image`

ACK timeout:

- Sender waits up to `1500 ms` for ACK.

Retry policy:

- Retry on timeout or retryable error.
- Max attempts: `5` (initial send + up to 4 retries).
- Delay before each retry (full-jitter exponential backoff):
  - base = `250 ms`
  - cap = `5000 ms`
  - formula for attempt `n` (starting at 1 for first retry):
    - `delay = random(0, min(cap, base * 2^n))`

Failure handling:

- If all attempts fail, mark event delivery as failed and continue session.
- Implementations may queue failed events for later reconnect replay (optional in v1).

## 6. Dedupe and Feedback-Loop Prevention

Both peers must implement:

- Recent `event_id` cache:
  - size: at least `512`
  - TTL: at least `10 minutes`
- Recent inbound `payload_hash` cache per source:
  - size: at least `64`
  - TTL: at least `30 seconds`

Rules:

- If `event_id` already exists in cache:
  - do not apply clipboard change again
  - send `ack` with `status=duplicate`
- If applying inbound clipboard content locally, mark origin as remote.
- Outbound watchers must not rebroadcast remote-origin clipboard updates.
- If origin tagging is unavailable, suppress outbound if payload hash matches a recent inbound hash.

## 7. Stale Event Rule

- Receiver computes `abs(now_ms - created_at_ms)`.
- If value is greater than `120000 ms` (2 minutes), reject event with `STALE_EVENT`.

## 8. Error Codes

Required codes:

| Code | Meaning | Retryable |
| --- | --- | --- |
| `AUTH_FAILURE` | Token missing/invalid/expired | no |
| `PAYLOAD_TOO_LARGE` | Payload exceeds declared limits (image > 10 MB, or configured text cap) | no |
| `UNSUPPORTED_TYPE` | Receiver does not support `event_type` or payload MIME | no |
| `STALE_EVENT` | Event timestamp outside stale window | no |

Recommended additional codes:

| Code | Meaning | Retryable |
| --- | --- | --- |
| `BAD_MESSAGE` | JSON/schema/field validation failure | no |
| `HASH_MISMATCH` | `payload_hash` does not match payload | no |
| `VERSION_MISMATCH` | Incompatible protocol major version | no |
| `TEMPORARY_UNAVAILABLE` | Receiver temporarily cannot process event | yes |

## 9. Version Compatibility Strategy

- `protocol_version` uses semantic `major.minor`.
- Major version mismatch:
  - receiver rejects with `VERSION_MISMATCH`.
- Same major, different minor:
  - receiver must ignore unknown object fields.
  - unknown `event_type` must return `UNSUPPORTED_TYPE`.
  - sender should only rely on behavior defined in the lower common minor version.
- v1 implementations should accept any `1.x` if schema validation passes and behavior is understood.

## 10. Example Exchange (Text Event)

Client -> Server:

```json
{
  "protocol_version": "1.0",
  "device_id": "android.pixel7.abc123",
  "event_id": "01JABW8P73QZZ0C3M4YH3DPVK2",
  "source": "android",
  "event_type": "set_text",
  "created_at_ms": 1760000000123,
  "payload_hash": "6439047f33bb1d758904d1f8602f8cf6fb4f6f3d8629ca6fe7ac11f2a1ca4674",
  "payload": {
    "mime_type": "text/plain",
    "text": "hello from android",
    "is_sensitive": false
  }
}
```

Server -> Client:

```json
{
  "protocol_version": "1.0",
  "device_id": "mac.studio.01",
  "event_id": "01JABW9J55AKB0Q6RYF7V5YSH9",
  "source": "mac",
  "event_type": "ack",
  "created_at_ms": 1760000000230,
  "payload_hash": "6c917ef6a2f145e3566abf89e3dcf1450360fbe78853e5c23138e69fc0ec4bdf",
  "payload": {
    "acked_event_id": "01JABW8P73QZZ0C3M4YH3DPVK2",
    "status": "accepted"
  }
}
```

## 11. Phase 1 Exit Check Mapping

This spec satisfies Phase 1 requirements by defining:

- versioned protocol doc under `docs/`
- envelope fields
- token auth transport
- text and image payload schemas
- ACK/retry/backoff behavior
- dedupe and feedback-loop rules
- required error codes
- version compatibility strategy
