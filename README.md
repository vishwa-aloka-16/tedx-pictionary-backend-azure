# TEDx Pictionary backend for Azure

Standalone FastAPI game backend. Includes the 50-class TensorFlow model, artwork
fonts/licenses and logos. Frontend and MongoDB artwork-sharing service are hosted
separately. Local secrets, leaderboard databases and backups are not included.

## Run locally

Use Python 3.14. From the repository root:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
Copy-Item backend/.env.example backend/.env
```

Configure `backend/.env`, including an `ADMIN_PASSWORD` of 12 to 256 characters,
then run:

```powershell
.venv/Scripts/python.exe -m uvicorn backend.app:app --host 0.0.0.0 --port 8000 --workers 1
```

Check `/health` after the model finishes loading. Frontend Admin clients must
support the password login and bearer-token session endpoints.

## Azure App Service (Linux, Code deployment)

1. Select Python 3.14 and connect this repository's `main` branch in Deployment
   Center. Build/deploy from the repository root.
2. Enable remote dependency installation with the app setting
   `SCM_DO_BUILD_DURING_DEPLOYMENT=true` when using Oryx/ZIP deployment.
3. Configure App Service environment variables (do not commit real secrets):

   | Name | Value |
   | --- | --- |
   | `ADMIN_PASSWORD` | A unique password, 12 to 256 characters |
   | `CORS_ORIGINS` | Your frontend HTTPS origin, no trailing slash; comma-separated for multiple origins |
   | `LEADERBOARD_PATH` | `/home/data/leaderboard.sqlite3` |
   | `ARTWORK_SHARE_URL` | `https://tedx-qr.onrender.com` or your sharing service origin |
   | `ARTWORK_SHARE_UPLOAD_KEY` | The sharing service's existing `UPLOAD_API_KEY` |

4. Set the startup command:

   ```bash
   python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000 --workers 1
   ```

5. Inspect Log stream and visit `https://<app-name>.azurewebsites.net/health`.
6. Set the separately deployed frontend's `VITE_API_URL` to the Azure backend
   origin and rebuild/redeploy that frontend.

Keep one worker and one instance: active games and Admin sessions are in memory.
Restarts end those sessions. SQLite persists only where the hosting environment
provides persistent storage; back up the database separately. No separate SQL
server is required for this SQLite implementation.

The exact dependency lock preserves the existing TensorFlow release candidate
and Keras nightly. Local checks use Windows/Python 3.14; validate installation,
model inference, WebSockets and memory limits on Azure Linux before live use.
Free-tier deployment is a trial, not a guarantee of sufficient gameplay capacity.

See [backend/DEPLOYMENT.md](backend/DEPLOYMENT.md) for dependency maintenance and
Admin-session behavior. All artwork resources resolve within `backend/assets`.

## Two-seat queue and reconnect

Only two player seats can be occupied system-wide. Additional players wait in a
FIFO queue and are admitted automatically when a seat is released. On a detected
disconnect, active gameplay pauses and the seat is reserved for 15 seconds.
Both players reconnect through a three-second countdown, retaining the same
round, points and remaining time. On expiry, the unfinished match is cancelled
and the remaining player returns to waiting.

Deploy the updated backend and frontend together. Keep one backend worker and
one instance; queue and active game state live in memory. The backend was
previously deployed manually: pushing this repository alone does not deploy it
unless you have since connected an Azure deployment workflow.

See [queue/reconnect QA notes](docs/QUEUE_AND_RECONNECT_QA.md),
[user flows](docs/HIGH_LEVEL_USER_FLOWS.md),
[scoring rules](docs/QA_SCORING_AND_RULES.md) and
[URLs/endpoints](docs/QA_ENDPOINTS_AND_URLS.md).
