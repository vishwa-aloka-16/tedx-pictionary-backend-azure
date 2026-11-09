# TEDx Pictionary — URLs and Endpoint Reference

Based on the current source code. Hostnames were supplied during deployment;
this document is a route inventory, not a live availability check.

## Frontend pages

| Page | URL |
| --- | --- |
| Game entry | https://yellow-cliff-05b33df00.8.azurestaticapps.net/ |
| Admin sign-in and settings | https://yellow-cliff-05b33df00.8.azurestaticapps.net/admin |
| Leaderboard and spectator display | https://yellow-cliff-05b33df00.8.azurestaticapps.net/leaderboard |
| Artwork page | `https://yellow-cliff-05b33df00.8.azurestaticapps.net/artwork/{artwork_id}` |

`/leaderbord` and `/leaderborad` are existing aliases for `/leaderboard`.
Registration, waiting, drawing and final results are application states at the
main URL; they are not separate `/register`, `/game` or `/results` pages.
Replace `{artwork_id}` with an actual completed-game artwork ID.

## Game backend

Base URL:

```text
https://tedx-ai-pictionary-backend-aqdec0akgdgtdfez.southeastasia-01.azurewebsites.net
```

Append each path below to that base. Frontend pages and backend API routes are
on different hosts; for example, frontend `/admin` is a page, while backend
`GET /admin` returns protected settings JSON.

| Method | Path | Purpose | Required input/access |
| --- | --- | --- | --- |
| GET | `/` | API identity/status | Public |
| GET | `/health` | Health, model and game configuration | Public |
| POST | `/admin/login` | Create Admin session | JSON password |
| POST | `/admin/logout` | Revoke Admin session | Admin bearer token |
| GET | `/admin` | Read models and settings | Admin bearer token |
| PUT | `/admin` | Update settings for new games | Admin bearer token and settings JSON |
| POST | `/admin/reset-dashboard` | Reset leaderboard and dashboard | Admin bearer token; destructive QA action |
| GET | `/leaderboard` | Global scores and game activity | Public |
| GET | `/leaderbord` | Leaderboard spelling alias | Public |
| GET | `/leaderborad` | Leaderboard spelling alias | Public |
| POST | `/predict` | Standalone model prediction; does not score a game | JSON canvas image |
| POST | `/matchmaking/join` | Join/resume a waiting game | JSON name; optional browser client ID |
| GET | `/matchmaking/queue/{queue_id}` | Queue position or assigned game/player | Valid queue ticket |
| POST | `/matchmaking/queue/{queue_id}/leave` | Leave queue or cancel a just-assigned seat | Valid queue ticket |
| GET | `/games/{game_id}` | Current game state | Query `player_id` |
| POST | `/games/{game_id}/ready` | Mark player ready | JSON player ID |
| POST | `/games/{game_id}/predict` | Judge a drawing during an active round | JSON player ID and canvas image |
| POST | `/games/{game_id}/restart` | Request rematch after final result | JSON player ID |
| POST | `/games/{game_id}/leave` | Leave game | JSON player ID |
| POST | `/games/{game_id}/round-drawing` | Attach final display drawing; no scoring | JSON player ID, event ID and canvas image |
| GET | `/round-drawings/{event_id}` | Temporary round drawing PNG | Public; valid stored drawing key |
| GET | `/artworks/{artwork_id}` | Artwork metadata and sharing status | Public; completed artwork ID |
| GET | `/artworks/{artwork_id}/image` | Render/preview artwork PNG | Public; completed artwork ID |
| GET | `/artworks/{artwork_id}/image?download=true` | Download artwork PNG | Public; completed artwork ID |
| GET | `/artworks/{artwork_id}/qr` | Generate QR PNG | Public; completed artwork ID; see note below |

With hosted sharing enabled, `/qr` uses the published image URL and returns 409
while upload is pending. With hosted sharing disabled, provide a URL-encoded
`frontend_origin` query value matching a configured allowed origin, such as
`https://yellow-cliff-05b33df00.8.azurestaticapps.net` (without a trailing slash).

Player IDs are session identifiers checked against game membership. They are not
Admin bearer tokens or signed player authentication credentials.

### API documentation URLs

- [Swagger UI](https://tedx-ai-pictionary-backend-aqdec0akgdgtdfez.southeastasia-01.azurewebsites.net/docs)
- [ReDoc](https://tedx-ai-pictionary-backend-aqdec0akgdgtdfez.southeastasia-01.azurewebsites.net/redoc)
- [OpenAPI JSON](https://tedx-ai-pictionary-backend-aqdec0akgdgtdfez.southeastasia-01.azurewebsites.net/openapi.json)
- `/docs/oauth2-redirect`: framework-provided Swagger authorization redirect helper.

### Request body examples

POST endpoints use `Content-Type: application/json`, unless explicitly described
as a raw PNG upload in the separate image service section.

**Join** — `POST /matchmaking/join`:

```json
{
  "name": "QA Player One",
  "client_id": "qa-browser-unique-id-0001"
}
```

When both seats are occupied, join returns `queued: true`, `queue_id` and
`position` instead of a game/player. Poll the queue endpoint until it returns
`queued: false` with the assigned `game` and `player_id`.

`name` is 1–24 characters. Optional `client_id` is 16–100 characters. Use a
different client ID for each independent test player. The response provides
`player_id` and `game.id`; use those actual values in subsequent requests.

**Ready / restart / leave**:

```json
{
  "player_id": "PLAYER_ID_FROM_JOIN"
}
```

**Standalone prediction** — `POST /predict`:

```json
{
  "image_data_url": "data:image/png;base64,BASE64_PNG_BYTES"
}
```

**Game prediction** — `POST /games/{game_id}/predict`:

```json
{
  "player_id": "PLAYER_ID_FROM_JOIN",
  "image_data_url": "data:image/png;base64,BASE64_PNG_BYTES"
}
```

**Final round drawing** — `POST /games/{game_id}/round-drawing`:

```json
{
  "player_id": "PLAYER_ID_FROM_JOIN",
  "event_id": "ROUND_EVENT_ID",
  "image_data_url": "data:image/png;base64,BASE64_PNG_BYTES"
}
```

Use the result event ID returned by the game. Only participants of that round may
submit. The data URL must contain a real PNG; the placeholders above are not valid
images. The final-drawing route limits the data URL to 500,000 characters.

**Admin login** — `POST /admin/login`:

```json
{
  "password": "YOUR_CONFIGURED_ADMIN_PASSWORD"
}
```

Use the returned `access_token` on subsequent Admin requests:

```http
Authorization: Bearer ACCESS_TOKEN_FROM_LOGIN
```

Do not include actual passwords or bearer tokens in QA documents, URLs or reports.

**Admin update** — `PUT /admin`:

```json
{
  "model_key": "tensorflow_50",
  "round_seconds": 40,
  "prediction_gap_seconds": 0.1,
  "required_hits": 3,
  "confidence_threshold": 0.6,
  "excluded_classes": ["helicopter", "bus", "broccoli", "giraffe", "grapes"]
}
```

Optional fields include `class_difficulties` (a mapping covering every model
class) and `cosmetic_draw_seconds_by_round` (six numbers, each 0–10). For validation
and omitted-field behavior, see [QA_SCORING_AND_RULES.md](QA_SCORING_AND_RULES.md).

## WebSockets

| Purpose | URL |
| --- | --- |
| Live leaderboard | `wss://tedx-ai-pictionary-backend-aqdec0akgdgtdfez.southeastasia-01.azurewebsites.net/ws/leaderboard` |
| Player game updates | `wss://tedx-ai-pictionary-backend-aqdec0akgdgtdfez.southeastasia-01.azurewebsites.net/ws/games/{game_id}?player_id={player_id}` |

Use a WebSocket client, not a normal HTTP GET. Game sockets expect client text
heartbeats within the 10-second receive timeout; the frontend sends every three
seconds. An `away` message signals screen lock/backgrounding when available.
An invalid game/player combination closes the game socket with code 4403.

## Separate QR image-sharing service

Base URL: `https://tedx-qr.onrender.com`

| Method | Path | Purpose/access |
| --- | --- | --- |
| GET | `/health` | Public image-service/database readiness |
| GET | `/images/{artwork_id}` | Public hosted PNG; this is the published QR destination |
| GET | `/images/{artwork_id}?download=true` | Public PNG download |
| PUT | `/images/{artwork_id}` | Private server-to-server PNG upload |

The PUT request requires `Authorization: Bearer UPLOAD_API_KEY`,
`Content-Type: image/png` and raw PNG bytes. This key is separate from Admin
session tokens. The image service validates IDs of 16–128 letters, digits,
underscores or hyphens; PNG uploads are limited to 8 MB and 4096 × 4096 pixels.

## QA usage notes

- Obtain real game/player/event/artwork IDs through normal game responses.
- A browser address bar sends GET; it cannot test POST/PUT actions by itself.
- Use Swagger, Postman or an equivalent API client for request-body testing.
- A frontend page loading does not prove backend connectivity; check actual API
  requests and WebSocket connections as well.
- `/admin/reset-dashboard` clears saved scores. Run only with intended test data.
- Temporary drawing/artwork URLs may expire or become unavailable after cache
  eviction/restart. Hosted image links use the separate sharing service.
- Admin endpoints require login; public pages and normal gameplay do not require
  the Admin password. CORS is a browser access policy, not authentication.
