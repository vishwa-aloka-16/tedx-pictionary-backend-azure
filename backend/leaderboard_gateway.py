"""MongoDB-backed leaderboard through the existing Node service; stdlib HTTP only."""
from contextlib import closing, contextmanager
import json
import logging
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class LeaderboardUnavailable(RuntimeError):
    def __init__(self, message="Leaderboard storage is temporarily unavailable.", status=503, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class LeaderboardGateway:
    def __init__(self, base_url, key, path, *, initialize_outbox=True):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password
                or parsed.path not in {'', '/'} or parsed.query or parsed.fragment or len(key) < 32):
            raise ValueError('Use the existing ARTWORK_SHARE_URL origin and ARTWORK_SHARE_UPLOAD_KEY for game data.')
        self.base_url, self.key, self.path = base_url.rstrip('/'), key, Path(path)
        self.generation = None
        self.revision = 0
        self.lock = threading.RLock()
        self.profiles = {}
        self.cached_rows = None
        self.cache_until = 0
        if not initialize_outbox:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS leaderboard_outbox (id TEXT PRIMARY KEY, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS leaderboard_delivery_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            yield db

    def request(self, method, path, payload=None, timeout=8):
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base_url + path, data=body, method=method,
            headers={'Authorization': f'Bearer {self.key}', 'Accept': 'application/json',
                     **({'Content-Type': 'application/json'} if body is not None else {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
                if not isinstance(data, dict):
                    raise ValueError('Invalid response')
                return data
        except urllib.error.HTTPError as error:
            try:
                code = json.load(error).get('code')
            except (ValueError, AttributeError):
                code = None
            error.close()
            status = error.code if error.code in {400, 401, 403, 404, 409} else 503
            raise LeaderboardUnavailable('Game data service rejected the request. Check service version and configuration.' if status != 503 else 'Leaderboard storage is temporarily unavailable.', status, code) from None
        except (OSError, ValueError):
            raise LeaderboardUnavailable() from None

    def prepare(self):
        revision = self.revision
        state = self.request('GET', '/game-data/health', timeout=60)
        if state.get('status') != 'ok' or not isinstance(state.get('generation'), str):
            raise LeaderboardUnavailable('The game data service is not ready. Deploy the updated Node service first.')
        if revision == self.revision:
            self.generation = state['generation']
        return {'status': 'ok'}

    def profile(self, name):
        if name not in self.profiles:
            revision = self.revision
            result = self.request('POST', '/game-data/players/resolve', {'name': name})
            if revision == self.revision:
                self.generation = result['generation']
            self.profiles[name] = result['emoji']
        return self.profiles[name]

    def rows(self):
        if self.cached_rows is None or time.monotonic() >= self.cache_until:
            revision = self.revision
            data = self.request('GET', '/game-data/leaderboard?limit=20')
            if revision != self.revision:
                raise LeaderboardUnavailable('Leaderboard was reset. Please refresh.')
            self.generation = data['generation']
            self.cached_rows = [{'player_name': p['name'], 'score': p['score'], 'emoji': p.get('emoji'),
                                 'games': p['games'], 'wins': p['wins']} for p in data['players']]
            self.cache_until = time.monotonic() + 2
        return [dict(row) for row in self.cached_rows]

    def enqueue(self, completion_id, players):
        if self.generation is None:
            raise LeaderboardUnavailable('Game storage has not been prepared.')
        payload = {'generation': self.generation, 'completed_at': time.time(),
                   'players': [{'name': p.name, 'score': p.score, 'wins': p.round_wins} for p in players]}
        with self.db() as db:
            db.execute('INSERT INTO leaderboard_outbox (id, payload) VALUES (?, ?) ON CONFLICT(id) DO NOTHING',
                       (completion_id, json.dumps(payload)))

    def pending_count(self):
        with self.db() as db:
            return db.execute('SELECT COUNT(*) FROM leaderboard_outbox').fetchone()[0]

    def _reset_intent(self):
        with self.db() as db:
            row = db.execute("SELECT value FROM leaderboard_delivery_state WHERE key = 'pending_reset'").fetchone()
        return json.loads(row[0]) if row else None

    def _apply_reset(self, intent):
        try:
            result = self.request('POST', '/game-data/reset', intent)
        except LeaderboardUnavailable as error:
            if error.code != 'generation_conflict':
                raise
            result = self.request('GET', '/game-data/health')
        self.revision += 1
        self.generation = result['generation']
        with self.db() as db:
            db.execute("DELETE FROM leaderboard_delivery_state WHERE key = 'pending_reset'")
            for completion_id, payload in db.execute('SELECT id, payload FROM leaderboard_outbox').fetchall():
                if json.loads(payload)['generation'] != self.generation:
                    db.execute('DELETE FROM leaderboard_outbox WHERE id = ?', (completion_id,))
        self.cached_rows = None
        return {'type': 'reset'}

    def reset(self):
        with self.lock:
            intent = self._reset_intent()
            if intent is None:
                self.prepare()
                intent = {'generation': self.generation, 'reset_id': secrets.token_urlsafe(24)}
                with self.db() as db:
                    db.execute("INSERT OR REPLACE INTO leaderboard_delivery_state (key, value) VALUES ('pending_reset', ?)", (json.dumps(intent),))
            self._apply_reset(intent)

    def flush(self):
        events = []
        with self.lock:
            intent = self._reset_intent()
            if intent:
                events.append(self._apply_reset(intent))
            with self.db() as db:
                pending = db.execute('SELECT id, payload, attempts FROM leaderboard_outbox WHERE next_attempt <= ? ORDER BY rowid LIMIT 10', (time.time(),)).fetchall()
            for completion_id, encoded, attempts in pending:
                try:
                    payload = json.loads(encoded)
                    receipt = self.request('PUT', '/game-data/matches/' + urllib.parse.quote(completion_id, safe=''), payload)
                    if (receipt.get('status') != 'saved' or receipt.get('completion_id') != completion_id
                            or receipt.get('generation') != payload['generation'] or not isinstance(receipt.get('rankings'), list)):
                        raise LeaderboardUnavailable('The game data service did not confirm the saved result.')
                except LeaderboardUnavailable as error:
                    if error.code == 'stale_generation':
                        with self.db() as db:
                            db.execute('DELETE FROM leaderboard_outbox WHERE id = ?', (completion_id,))
                        events.append({'type': 'discarded', 'completion_id': completion_id})
                        continue
                    with self.db() as db:
                        db.execute('UPDATE leaderboard_outbox SET attempts = attempts + 1, next_attempt = ? WHERE id = ?',
                                   (time.time() + min(30, 2 ** min(attempts + 1, 5)), completion_id))
                    logging.getLogger(__name__).warning('Leaderboard save remains pending (service status %s).', error.status)
                    break
                with self.db() as db:
                    db.execute('DELETE FROM leaderboard_outbox WHERE id = ?', (completion_id,))
                self.generation = receipt['generation']
                self.cached_rows = None
                events.append({'type': 'saved', **receipt})
        return events
