# Notifications and privacy

Koemichi writes notification events to its SQLite outbox. The worker sends them to Pushover independently of transcript classification and NoteDiscovery writes, so a Pushover outage does not block note processing. Configure `PUSHOVER_API_TOKEN` and `PUSHOVER_USER_KEY`, or set `NOTIFICATION_MODE=off`.

| Mode | Behavior |
|---|---|
| `normal` | A notice when a transcript is accepted, another when its Memo, Journal, or Todo workflow completes, and an urgent notice for a terminal processing failure. Memo and Journal completion notices include the NoteDiscovery path and saved content; Todo notices include the created Vikunja task title. |
| `debug` | Normal notices plus low-priority processing, retry, and recovery details. Completion notices also include classifier information; terminal failures remain urgent. |
| `off` | No notifications are queued or delivered; Pushover credentials are not required. |

Pushover messages are limited to 1,024 characters. Longer messages are visibly marked as truncated. Memo content or the new Journal transcript remains in the Koemichi database; the outbox stores the notification text that will be sent.

Transcript and note content are personal data. In normal/debug modes, text is sent to Pushover and may appear on a device lock screen. Protect the Pushover account and devices accordingly. The database also contains transcripts, generated drafts, note paths, and notification state; protect its data directory and backups.

Koemichi receives transcripts after Koebako has recorded and transcribed the audio. Upload and STT-stage notices therefore come from Koebako; Koemichi's accepted notice means the transcript reached its receiver.
