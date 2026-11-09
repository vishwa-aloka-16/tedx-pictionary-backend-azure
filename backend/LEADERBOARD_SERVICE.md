# Leaderboard persistence through the existing Node service

## Architecture

The Python game server calculates points and controls live matches. It sends
completed results over authenticated HTTPS to `qr-to-image-hosted`, which stores
them using its existing Mongoose/MongoDB connection. No PyMongo, dnspython, new
Python runtime package or new Node environment variable is required.

With `ARTWORK_SHARE_URL` set, remote storage is enabled automatically. Python's
`ARTWORK_SHARE_UPLOAD_KEY` authenticates both image and game-data requests against
the Node service's existing `UPLOAD_API_KEY`. The frontend never receives that key.
With the URL blank, the existing local SQLite leaderboard is used instead.

The Node service adds these collections beside its unchanged artwork collection:

- `game_player_profiles`: exact username and unique, persistent emoji.
- `game_matches`: one immutable document per completion, containing both players'
  server-calculated scores, round wins, completion time and leaderboard generation.
- `game_leaderboard_state`: the current generation used to clear the visible
  leaderboard without allowing old pending saves to restore cleared scores.

Standings are derived from completed match facts: maximum single-game score per
username, binary name ordering for ties, and lifetime round-win/game counts.
Global player positions include people beyond the displayed top twenty. Duplicate
submissions with identical content reuse the completion ID; conflicting content
is rejected. A rematch uses its own new artwork/completion ID.

## Deploy in this order

1. Deploy the updated **Node/QR service first**, using its existing `MONGODB_URI`,
   `UPLOAD_API_KEY`, `PUBLIC_BASE_URL` and `PORT` settings. Its startup creates the
   new game-data indexes and generation record before listening. Existing image
   routes keep their behavior.
2. Before new games, optionally import existing SQLite scores/profiles as described
   below. There is no automatic upload of historical player data.
3. Deploy the Python backend. Keep the existing URL and key settings and ensure
   `LEADERBOARD_PATH=/home/data/leaderboard.sqlite3` in Azure. It now holds pending
   deliveries rather than being the remote leaderboard's source of truth. If the
   path is not explicitly configured, Linux Azure App Service uses that `/home`
   default automatically; local development still uses `backend/leaderboard.sqlite3`.
4. Deploy the frontend. Its existing `VITE_API_URL` remains the Python backend
   origin. No frontend URL/key for the Node service is needed.

Keep one Python worker and one instance. Active games, queues and Admin settings
are still in memory; MongoDB storage does not make those sessions restart-safe.

## Readiness at initial player entry

Selecting **Let's play** opens registration and calls `POST /matchmaking/prepare`.
That request wakes Python if needed and checks the Node service's authenticated
`/game-data/health`, including MongoDB readiness. Players can enter a name while
waiting; joining is enabled once readiness succeeds. Requests allow a cold start,
retry within a two-minute window and offer a manual retry if services remain down.
No periodic keep-alive is added while the site is idle.

The existing game-start image-service warm-up remains an additional safeguard.
Live round logic never waits for MongoDB score storage or a drawing upload.

## Save delivery and failure behavior

At completion, Python writes one payload to `leaderboard_outbox` in SQLite before
showing the final result. The final screen labels the global ranking as pending
until a background worker receives a valid saved receipt from Node. The worker
checks every two seconds and uses bounded HTTP timeouts with retry backoff up to
30 seconds. A lost acknowledgement can be retried without duplicate game counts.

The outbox is replayed after Python restarts; acknowledged scores already reside
in MongoDB. This guarantee depends on retaining the SQLite outbox on persistent
storage. A temporary filesystem, disk failure or deletion of the outbox can still
lose submissions that MongoDB has not acknowledged. Back up persistent storage
and MongoDB according to the event's requirements.

HTTP/database failures do not fabricate a global position. Permanent payload/key
conflicts remain pending for operator correction; an explicit stale-generation
response after reset discards only the obsolete submission. Logs identify delayed
delivery without printing keys or database credentials.

Leaderboard reads are briefly cached to reduce repeated work from display sockets.
Successful saves/reset invalidate the cache. Existing final announcements update
when the confirmed rankings arrive, including personal-best celebrations.

Admin reset changes the current generation and retains profiles. Old match facts
are archived under their previous generation rather than immediately deleted.
Only current-generation records participate in standings. A persisted reset intent
can be retried after a lost response/restart without erasing subsequently saved
games. Active games completing after a confirmed reset use the new generation.

## Private Node endpoints

All `/game-data/*` routes require the **existing** upload bearer key:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/game-data/health` | MongoDB-backed readiness and current generation |
| POST | `/game-data/players/resolve` | Retrieve/assign persistent avatar |
| PUT | `/game-data/matches/{completion_id}` | Save immutable completion or return its receipt |
| GET | `/game-data/leaderboard?limit=20` | Ranked scores and current generation |
| POST | `/game-data/reset` | Idempotent, generation-checked reset |
| POST | `/game-data/import` | Explicit legacy import into an empty generation |

These routes are server-to-server; do not call them from the frontend or publish
the private key in documentation. Existing Python leaderboard/Admin endpoints
remain the browser-facing interface. Image GET routes remain public for QR scans.

## Optional SQLite import

Stop gameplay and keep a backup of the old SQLite file. Use the existing Python
environment with the correct service URL/key. Preview without uploading:

```powershell
backend/.venv/Scripts/python.exe -m backend.tools.import_leaderboard backend/leaderboard.sqlite3
```

To upload explicitly:

```powershell
backend/.venv/Scripts/python.exe -m backend.tools.import_leaderboard backend/leaderboard.sqlite3 --execute
```

On Azure/Linux, use the active Python interpreter and actual persistent source
path instead. Source SQLite is opened read-only, including during execution. The
tool preserves known best scores, historical totals/wins/game counts and available
avatars. Unknown best scores from old aggregate-only multi-game data are not
misrepresented as a single-game score. Repeating an identical import is harmless;
mixing a different import into populated history is refused. Profile collisions
receive unused avatars. Imports are limited to 500 score/profile entries and a
256 KB request body; a larger dataset needs a separate batch migration.

## Verification

Local tests cover retries, restart replay, lost acknowledgements, reset generations,
scores/ranks/avatars, existing image APIs, entry readiness and gameplay regression.
Node tests use an in-memory repository; Python tests simulate HTTP responses. They
do not establish a live Atlas connection or write production scores. Before going
live, verify actual Node/MongoDB connectivity, complete a test game, restart Python
and Node, and confirm the saved leaderboard and emojis remain available.
