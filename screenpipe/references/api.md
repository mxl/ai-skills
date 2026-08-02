# Screenpipe Meeting API Contract

Verified against current Screenpipe source on 2026-08-02.

## Authentication

Local API base: `http://localhost:3030`.

Authenticated endpoints require:

```text
Authorization: Bearer $SCREENPIPE_LOCAL_API_KEY
```

`GET /health` is unauthenticated. Missing or rejected credentials return HTTP
403 for meeting endpoints.

## Endpoints

### `GET /meetings`

Query parameters:

- `start_time`: optional flexible datetime;
- `end_time`: optional flexible datetime;
- `q`: case-insensitive substring over title, attendees, note, and meeting app;
- `limit`: defaults to 20;
- `offset`: defaults to 0.

Response is a JSON array of meeting records.

### `GET /meetings/:id`

Returns one meeting record or HTTP 404.

Meeting record fields:

```json
{
  "id": 42,
  "meeting_start": "RFC3339",
  "meeting_end": "RFC3339 or null",
  "meeting_app": "zoom.us",
  "title": "string or null",
  "attendees": "string or null",
  "note": "string or null",
  "detection_source": "string",
  "created_at": "RFC3339"
}
```

### `GET /meetings/:id/transcript`

Returns a JSON array of transcript segments. Segment serialization uses camel
case:

```json
{
  "id": 1,
  "meetingId": 42,
  "source": "live or background",
  "provider": "string",
  "model": "string or null",
  "itemId": "string",
  "deviceName": "string",
  "deviceType": "input or output",
  "audioTranscriptionId": 1,
  "audioChunkId": 1,
  "audioFilePath": "/path or null",
  "speakerId": 1,
  "speakerName": "string or null",
  "transcript": "verbatim text",
  "capturedAt": "RFC3339",
  "createdAt": "RFC3339"
}
```

## Ordering And Deduplication

Screenpipe merges live meeting segments with background audio transcriptions.
Server query:

1. constrains background rows to the meeting time window;
2. excludes empty transcriptions, cloud-only audio, and hallucinated speakers;
3. drops a background row when a live segment from the same device direction is
   within 15 seconds;
4. orders by `captured_at`, then live before background, then row ID.

Consumers should preserve returned order and should not perform additional
semantic deduplication.

## Canonical Mapping

| Canonical field | Screenpipe source |
| --- | --- |
| `source` | constant `screenpipe` |
| `title` | `meeting.title`, fallback to `<meeting_app> meeting` |
| `started_at` | `meeting.meeting_start` |
| `ended_at` | `meeting.meeting_end` or empty string |
| `participants` | conservative parsing of `meeting.attendees` |
| `transcript[].started_at` | `capturedAt` |
| `transcript[].ended_at` | empty string; no reliable end is exposed |
| `transcript[].speaker` | `speakerName` or empty string |
| `transcript[].text` | exact `transcript` value |
| `notes` | non-empty `meeting.note` |
| `resources` | `screenpipe://meeting/<id>` |

Do not map `deviceName` to speaker identity. Do not derive segment end times from
the next segment because gaps and overlapping input/output capture make that
inference unreliable.
