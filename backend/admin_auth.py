"""Password-gated Admin sessions; no credentials are returned to the browser."""
from collections import OrderedDict, deque
import hashlib
import hmac
import math
import secrets
import threading
import time

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import ADMIN_PASSWORD

SESSION_SECONDS = 8 * 60 * 60
FAILURE_WINDOW_SECONDS = 60
MAX_FAILURES = 5


class AdminAuth:
    def __init__(self, password, clock=time.monotonic):
        self.configured = 12 <= len(password) <= 256
        self.password_hash = hashlib.sha256(password.encode()).digest()
        self.clock = clock
        self.sessions = OrderedDict()
        self.failures = OrderedDict()
        self.lock = threading.Lock()

    def _check_configuration(self):
        if not self.configured:
            raise HTTPException(503, "Admin access is disabled. Configure ADMIN_PASSWORD with 12–256 characters on the backend.")

    def login(self, password, client):
        self._check_configuration()
        now = self.clock()
        with self.lock:
            failures = self.failures.setdefault(client, deque())
            self.failures.move_to_end(client)
            while failures and failures[0] <= now - FAILURE_WINDOW_SECONDS:
                failures.popleft()
            while len(self.failures) > 1024:
                self.failures.popitem(last=False)
            if len(failures) >= MAX_FAILURES:
                retry = max(1, math.ceil(failures[0] + FAILURE_WINDOW_SECONDS - now))
                raise HTTPException(429, "Too many login attempts. Try again shortly.", headers={"Retry-After": str(retry)})
            if not hmac.compare_digest(self.password_hash, hashlib.sha256(password.encode()).digest()):
                failures.append(now)
                raise HTTPException(401, "Incorrect Admin password.")
            self.failures.pop(client, None)
            for key, expires in list(self.sessions.items()):
                if expires <= now:
                    self.sessions.pop(key, None)
            if len(self.sessions) >= 128:
                self.sessions.popitem(last=False)
            token = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).digest()] = now + SESSION_SECONDS
            return {"access_token": token, "token_type": "bearer", "expires_in": SESSION_SECONDS}

    def require(self, token):
        self._check_configuration()
        if token:
            key = hashlib.sha256(token.encode()).digest()
            with self.lock:
                expires = self.sessions.get(key, 0)
                if expires > self.clock():
                    return token
                self.sessions.pop(key, None)
        raise HTTPException(401, "Admin login required or session expired.", headers={"WWW-Authenticate": "Bearer"})

    def logout(self, token):
        with self.lock:
            self.sessions.pop(hashlib.sha256(token.encode()).digest(), None)


admin_auth = AdminAuth(ADMIN_PASSWORD)
bearer = HTTPBearer(auto_error=False)


def require_admin(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    return admin_auth.require(credentials.credentials if credentials else None)
