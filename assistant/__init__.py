"""Assistant layer: everything about your time, not your training.

- `calendars` — read-only calendar feeds (school, personal) stored as fixed events.
- `availability` — busy days, free slots, exam detection built on those events.
- `schedule` — moving/pushing planned sessions with preview and undo.
- `routes` — the `/api/assistant/*` HTTP API.

Compass's training code talks to this package only through `availability`
(how busy is a day, where are the free slots) and `schedule`; the assistant reads
workouts as just another source of events. Keep that boundary so this package can
become its own service later without a rewrite.
"""
