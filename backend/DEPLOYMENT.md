# Reproducible backend installation and Admin access

## Dependency baseline

The runtime lock in `backend/requirements.txt` pins direct **and transitive**
dependencies to exact versions. Root `requirements.txt` forwards to that lock,
so Azure/Oryx repository-root builds and local installs use the same versions.
`backend/requirements.in` lists the direct dependencies for maintenance.

The tested baseline is **CPython 3.14 on Windows**. `.python-version` records that
minor version; select Python 3.14 explicitly in Azure's runtime configuration.
Linux wheels/platform behavior must also be validated on Azure before launch.
The Linux-only Uvicorn extra `uvloop` is separately pinned; it is not installed
or tested on Windows. Windows-only `colorama` is conditional.

This change preserves the already-tested **TensorFlow 2.22.0rc0** and
**keras-nightly 3.16.0.dev2026092404**. Pinning a prerelease makes its selection
repeatable; it does not turn it into a stable release. A move to stable
TensorFlow/Keras is a separate, deliberate compatibility change requiring model
loading, prediction, and gameplay checks. Do not silently upgrade these packages
or install an additional conflicting `keras` distribution.

From the repository root:

```powershell
python -m venv backend/.venv
backend/.venv/Scripts/python.exe -m pip install -r requirements.txt
```

On Linux, use `backend/.venv/bin/python` in place of the Windows executable.
Do not copy a Windows virtual environment to Azure. Deploy source/model/assets,
and install the locked dependencies on the target platform.

To maintain the lock after deliberately testing updated versions:

```powershell
backend/.venv/Scripts/python.exe backend/tools/lock_requirements.py
```

The generator follows only the installed application's dependency tree. It does
not freeze unrelated tools from the virtual environment or include PyTorch.
Review the Linux-only `uvloop` pin when updating Uvicorn. Test the resulting
requirements on the deployment platform as well as locally.

Exact pins prevent automatic version drift. This is not an artifact-hash lock or
a guarantee of bit-identical Windows/Linux binaries. For stricter company supply
chain controls, produce a target-platform wheelhouse/hash lock in the company CI.
Frontend and QR-service dependencies already have `package-lock.json`; deploy
them with `npm ci` instead of updating dependencies during a production build.

## Configure the Admin password

Set this **only in the Python backend's environment**:

```dotenv
ADMIN_PASSWORD=choose-a-unique-password-with-at-least-12-characters
```

Accepted length is 12–256 characters. Local `.env` files are ignored by Git.
A generated password has been added to the local `backend/.env` if one was not
already configured. Open that file locally to view or replace it. If a password
contains dotenv-special characters such as `#`, quote it according to dotenv
syntax. Never add the password to frontend code, `VITE_` variables, URLs or logs.

In Azure App Service, add `ADMIN_PASSWORD` to the backend app's environment
settings. Hosting environment variables take precedence over the `.env` file.
Restart the backend after changing the password. Deploy the updated frontend
alongside the backend; older Admin clients will receive 401 responses.

## Admin behavior

- `/admin` on the frontend shows a password sign-in form before loading settings.
- `POST /admin/login` validates the configured password and returns an opaque
  random bearer token. Login responses are not cacheable.
- `GET /admin`, `PUT /admin`, `POST /admin/reset-dashboard`, and
  `POST /admin/logout` require a valid token on the backend.
- Incorrect/missing/expired tokens receive 401. Five failed password attempts
  per client address within a minute cause temporary 429 responses.
- Blank/short/overlong backend password configuration disables Admin with 503;
  it never makes Admin public. Player, leaderboard and artwork routes remain
  accessible without the Admin password.
- Sessions last eight hours, are stored only in backend memory, and are revoked
  individually on logout. Backend restart/password-change restart invalidates
  all existing Admin sessions. Keep one backend worker/instance as required by
  the existing in-memory game architecture.
- The frontend keeps the token in tab session storage, with an in-memory fallback
  if storage is unavailable. It does not store the password. Use HTTPS in hosted
  environments to protect credentials and bearer tokens in transit.

This shared Admin password does not introduce company SSO, player authentication,
durable game recovery, or persistence of Admin settings. Those are separate
production requirements.
