import asyncio
from contextlib import closing
from collections import OrderedDict
import math
import random
import secrets
import sqlite3
import logging
import time
import threading
from dataclasses import (
    dataclass,
    field,
)
from typing import Any
from .config import LEADERBOARD_PATH, ARTWORK_SHARE_URL, ARTWORK_SHARE_UPLOAD_KEY
from .leaderboard_gateway import LeaderboardGateway, LeaderboardUnavailable
from .difficulty import LEVELS, default_difficulties, round_difficulty
from .artwork import ArtworkStore

from fastapi import (
    HTTPException,
    WebSocket,
)


MAX_PLAYERS = 2
DISCONNECT_GRACE_SECONDS = 15
QUEUE_TTL_SECONDS = 45
MAX_QUEUE_SIZE = 200
PLAYER_EMOJIS = ["\U0001f98a", "\U0001f43c", "\U0001f428", "\U0001f431", "\U0001f436",
                 "\U0001f438", "\U0001f981", "\U0001f42f", "\U0001f984", "\U0001f419",
                 "\U0001f989", "\U0001f427", "\U0001f435", "\U0001f430", "\U0001f43b",
                 "\U0001f422", "\U0001f42c", "\U0001f98b", "\U0001f41d", "\U0001f996"]
# A wider set keeps avatars distinct beyond the first twenty usernames.
PLAYER_EMOJIS = list(dict.fromkeys(PLAYER_EMOJIS + [chr(codepoint)
    for start, end in ((0x1F400, 0x1F43E), (0x1F600, 0x1F637), (0x1F347, 0x1F37F),
                       (0x1F680, 0x1F6A5), (0x1F330, 0x1F33C), (0x1F300, 0x1F320))
    for codepoint in range(start, end + 1)]))
MAX_ROUNDS = 6
ROUND_SECONDS = 40
COUNTDOWN_SECONDS = 3
PROMPT_DELIVERY_LEAD_SECONDS = 0.75
ROUND_RESULT_SECONDS = 6
COSMETIC_DRAW_SECONDS = 1.5
DEFAULT_COSMETIC_DRAW_SECONDS_BY_ROUND = (0.5, 0.5, 1, 1.5, 3, 3)
DRAWING_UPLOAD_GRACE_SECONDS = 1

PREDICTION_GAP_SECONDS = 0.1
CONFIDENCE_THRESHOLD = 0.6
REQUIRED_CONSECUTIVE_HITS = 3
DEFAULT_MODEL_KEY = "tensorflow_50"
DEFAULT_EXCLUDED_CLASSES = {"helicopter", "bus", "broccoli","giraffe","grapes"}
MINIMUM_JUDGE_SECONDS = 2.5
FINAL_ROUND_DRAW_SECONDS = 3.5
LATE_ROUND_FRACTION = 0.8


@dataclass
class Player:
    id: str
    name: str
    seat: int
    client_id: str | None = None
    emoji: str | None = None
    connected: bool = False
    disconnected_until: float | None = None

    ready: bool = False

    score: int = 0
    round_wins: int = 0

    last_guess: str = ""
    confidence: float = 0.0

    predictions_seen: int = 0
    consecutive_hits: int = 0

    recognized_at: (
        float | None
    ) = None


@dataclass
class Game:
    id: str
    artwork_id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    round_start_event_id: str | None = None
    required_hits: int = REQUIRED_CONSECUTIVE_HITS
    prediction_gap_seconds: float = PREDICTION_GAP_SECONDS
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    model_key: str = DEFAULT_MODEL_KEY
    classes: list[str] = field(default_factory=list)
    class_difficulties: dict[str, str] = field(default_factory=dict)
    round_seconds: int = ROUND_SECONDS
    cosmetic_draw_seconds_by_round: list[float] = field(default_factory=lambda: list(DEFAULT_COSMETIC_DRAW_SECONDS_BY_ROUND))

    phase: str = "WAITING"
    paused_phase: str | None = None
    paused_at: float | None = None
    paused_remaining_seconds: float | None = None
    result_transition_at: float | None = None
    cancellation_message: str = ""

    players: dict[
        str,
        Player,
    ] = field(default_factory=dict)

    round_number: int = 0

    prompt: str = ""
    previous_prompt: str = ""
    used_prompts: set[str] = field(default_factory=set)

    countdown_ends_at: (
        float | None
    ) = None

    round_started_at: (
        float | None
    ) = None

    round_deadline: (
        float | None
    ) = None

    round_winner_id: (
        str | None
    ) = None

    round_points: int = 0

    created_at: float = field(
        default_factory=time.time
    )

    transition_token: int = 0
    leaderboard_recorded: bool = False
    leaderboard_save_status: str = "not_saved"
    global_rankings: dict[str, dict] = field(default_factory=dict)
    round_winners: list[dict[str, Any]] = field(
        default_factory=list
    )

    @property
    def max_rounds(self) -> int:
        return min(MAX_ROUNDS, len(set(self.classes)))


@dataclass
class QueueEntry:
    id: str
    name: str
    model_key: str
    client_id: str | None
    last_seen: float = field(default_factory=time.time)


class GameManager:
    def __init__(
        self,
        available_classes: list[str],
        classes_by_model: dict[str, list[str]],
    ):
        self.classes = (
            available_classes.copy()
        )
        self.classes_by_model = {
            model_key: classes.copy()
            for model_key, classes
            in classes_by_model.items()
        }
        self.model_key = DEFAULT_MODEL_KEY if DEFAULT_MODEL_KEY in self.classes_by_model else next(iter(self.classes_by_model))
        self.round_seconds = ROUND_SECONDS
        self.cosmetic_draw_seconds_by_round = list(DEFAULT_COSMETIC_DRAW_SECONDS_BY_ROUND)
        self.prediction_gap_seconds = PREDICTION_GAP_SECONDS
        self.required_hits = REQUIRED_CONSECUTIVE_HITS
        self.confidence_threshold = CONFIDENCE_THRESHOLD
        self.excluded_classes: set[str] = DEFAULT_EXCLUDED_CLASSES.intersection(self.classes_by_model[self.model_key])
        self.difficulties_by_model = {
            key: default_difficulties(classes)
            for key, classes in self.classes_by_model.items()
        }
        self.dashboard_reset_at = 0.0
        self.admin_settings_lock = threading.RLock()
        self.admin_settings_loaded = False
        self.leaderboard_subscribers: set[asyncio.Queue] = set()
        self.disconnect_tasks = {}
        self.waiting_queue: OrderedDict[str, QueueEntry] = OrderedDict()
        self.queue_assignments: dict[str, tuple[str, str]] = {}
        self.connection_tasks: set[asyncio.Task] = set()
        # Cosmetic thumbnails only. Bounded RAM cache, never part of SQLite.
        self.round_drawings: OrderedDict[str, bytes] = OrderedDict()
        self.artworks = ArtworkStore()

        self.games: dict[
            str,
            Game,
        ] = {}

        self.game_locks: dict[
            str,
            asyncio.Lock,
        ] = {}

        self.connections: dict[
            str,
            list[
                tuple[
                    WebSocket,
                    str,
                ]
            ],
        ] = {}

        self.matchmaking_lock = (
            asyncio.Lock()
        )
        self.leaderboard_path = LEADERBOARD_PATH
        self.initialize_leaderboard()

    def initialize_leaderboard(self):
        self.leaderboard_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.leaderboard_path)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS leaderboard (
                    player_name TEXT PRIMARY KEY,
                    score INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    games INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            columns = {row[1] for row in connection.execute("PRAGMA table_info(leaderboard)")}
            if "best_score" not in columns:
                connection.execute("ALTER TABLE leaderboard ADD COLUMN best_score INTEGER")
                # A lifetime total only represents a known single-game score
                # when exactly one game was recorded. Preserve other totals.
                connection.execute("UPDATE leaderboard SET best_score = score WHERE games = 1")
            connection.execute("CREATE TABLE IF NOT EXISTS player_profiles (player_name TEXT PRIMARY KEY, emoji TEXT NOT NULL)")
            seen = set()
            for name, emoji in connection.execute("SELECT player_name, emoji FROM player_profiles ORDER BY rowid").fetchall():
                if emoji in seen:
                    emoji = self.unused_emoji(connection)
                    connection.execute("UPDATE player_profiles SET emoji = ? WHERE player_name = ?", (emoji, name))
                seen.add(emoji)
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS unique_player_emoji ON player_profiles (emoji)")
            for (name,) in connection.execute(
                "SELECT player_name FROM leaderboard WHERE player_name NOT IN (SELECT player_name FROM player_profiles)"
            ).fetchall():
                self.assign_emoji(connection, name)
        self.leaderboard_gateway = (LeaderboardGateway(ARTWORK_SHARE_URL, ARTWORK_SHARE_UPLOAD_KEY, self.leaderboard_path)
                                    if ARTWORK_SHARE_URL else None)

    async def prepare_entry(self):
        gateway = getattr(self, 'leaderboard_gateway', None)
        if gateway:
            await asyncio.to_thread(gateway.prepare)
        await asyncio.to_thread(self.ensure_admin_settings)
        return {"status": "ok"}

    def ensure_admin_settings(self):
        with self.admin_settings_lock:
            gateway = getattr(self, 'leaderboard_gateway', None)
            if self.admin_settings_loaded or not gateway:
                return
            result = gateway.request('GET', '/game-data/settings', timeout=60)
            if 'settings' not in result:
                raise LeaderboardUnavailable('Invalid saved admin settings response.')
            saved = result['settings']
            if saved is not None:
                try:
                    from .schemas import AdminSettingsRequest
                    validated = AdminSettingsRequest.model_validate(saved)
                    self._update_admin_settings(**validated.model_dump(), persist=False)
                except (ValueError, TypeError, HTTPException):
                    raise LeaderboardUnavailable('Saved admin settings are invalid; please check game_admin_settings in MongoDB.') from None
            self.admin_settings_loaded = True

    async def restore_admin_settings_loop(self):
        while True:
            try:
                await asyncio.to_thread(self.ensure_admin_settings)
                return
            except LeaderboardUnavailable:
                logging.getLogger(__name__).warning('Admin settings restoration delayed; retrying.')
                await asyncio.sleep(5)

    def apply_dashboard_reset(self):
        self.round_drawings.clear()
        self.artworks.clear()
        self.dashboard_reset_at = time.time()

    async def deliver_leaderboard(self):
        gateway = getattr(self, 'leaderboard_gateway', None)
        if not gateway:
            return
        try:
            events = await asyncio.to_thread(gateway.flush)
        except (LeaderboardUnavailable, sqlite3.Error, OSError, ValueError, KeyError) as error:
            logging.getLogger(__name__).warning('Leaderboard delivery delayed (%s).', type(error).__name__)
            return
        for event in events:
            if event['type'] == 'saved' and event.get('generation') != gateway.generation:
                event = {**event, 'type': 'discarded', 'rankings': []}
            if event['type'] == 'reset':
                self.apply_dashboard_reset()
            for game in list(self.games.values()):
                if game.artwork_id == event.get('completion_id'):
                    game.leaderboard_save_status = 'saved' if event['type'] == 'saved' else 'reset'
                    rankings = {p['name']: p for p in event.get('rankings', [])}
                    game.global_rankings = {p.id: rankings[p.name] for p in game.players.values() if p.name in rankings}
                    await self.broadcast(game)
            for queue in self.leaderboard_subscribers:
                if not queue.full():
                    queue.put_nowait(True)

    async def leaderboard_delivery_loop(self):
        while True:
            await self.deliver_leaderboard()
            await asyncio.sleep(2)

    @staticmethod
    def unused_emoji(connection):
        used = {row[0] for row in connection.execute("SELECT emoji FROM player_profiles")}
        available = [emoji for emoji in PLAYER_EMOJIS if emoji not in used]
        if not available:
            raise HTTPException(409, "All unique avatars are assigned. Existing players can still use their usernames.")
        return random.choice(available)

    @staticmethod
    def assign_emoji(connection, name):
        row = connection.execute("SELECT emoji FROM player_profiles WHERE player_name = ?", (name,)).fetchone()
        if row:
            return row[0]
        emoji = GameManager.unused_emoji(connection)
        connection.execute("INSERT INTO player_profiles (player_name, emoji) VALUES (?, ?) ON CONFLICT(player_name) DO NOTHING", (name, emoji))
        return connection.execute("SELECT emoji FROM player_profiles WHERE player_name = ?", (name,)).fetchone()[0]

    def player_emoji(self, name):
        gateway = getattr(self, 'leaderboard_gateway', None)
        if gateway:
            return gateway.profile(name)
        with closing(sqlite3.connect(self.leaderboard_path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.assign_emoji(connection, name)

    def record_leaderboard(self, game: Game):
        if game.leaderboard_recorded:
            return
        gateway = getattr(self, 'leaderboard_gateway', None)
        if gateway:
            gateway.enqueue(game.artwork_id, list(game.players.values()))
            game.leaderboard_recorded = True
            game.leaderboard_save_status = 'pending'
            game.global_rankings = {}
            return

        with closing(sqlite3.connect(self.leaderboard_path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = dict(connection.execute("SELECT player_name, best_score FROM leaderboard"))
            for player in game.players.values():
                player.emoji = self.assign_emoji(connection, player.name)
                connection.execute(
                    """
                    INSERT INTO leaderboard (player_name, score, wins, games, best_score)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(player_name) DO UPDATE SET
                        score = score + excluded.score,
                        wins = wins + excluded.wins,
                        games = games + 1,
                        best_score = MAX(COALESCE(best_score, excluded.best_score), excluded.best_score)
                    """,
                    (
                        player.name,
                        player.score,
                        player.round_wins,
                        player.score,
                    ),
                )

            ranked = connection.execute(
                "SELECT player_name, best_score FROM leaderboard WHERE best_score IS NOT NULL "
                "ORDER BY best_score DESC, player_name ASC"
            ).fetchall()
            positions = {name: (index + 1, score) for index, (name, score) in enumerate(ranked)}
            game.global_rankings = {
                player.id: {"name": player.name, "rank": positions[player.name][0],
                            "high_score": positions[player.name][1],
                            "new_high_score": player.score > (previous.get(player.name) or 0)
                            and player.score == positions[player.name][1]}
                for player in game.players.values()
            }
        game.leaderboard_recorded = True
        game.leaderboard_save_status = 'saved'

    def leaderboard_rows(self):
        gateway = getattr(self, 'leaderboard_gateway', None)
        if gateway:
            return gateway.rows()
        with closing(sqlite3.connect(self.leaderboard_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT leaderboard.player_name, best_score AS score, wins, games, player_profiles.emoji
                FROM leaderboard JOIN player_profiles USING (player_name)
                WHERE best_score IS NOT NULL
                ORDER BY best_score DESC, leaderboard.player_name ASC
                LIMIT 20
                """
            ).fetchall()
        return rows

    def leaderboard(self, rows=None) -> dict[str, Any]:
        if rows is None:
            rows = self.leaderboard_rows()
        now = time.time()

        players = [
            {
                "rank": rank,
                "name": row["player_name"],
                "emoji": row["emoji"] if "emoji" in row.keys() else None,
                "score": row["score"],
                "wins": row["wins"],
                "games": row["games"],
            }
            for rank, row in enumerate(rows, start=1)
        ]

        games = [
            {
                "id": game.id,
                "status": (
                    "in_progress"
                    if game.phase != "FINAL_RESULT"
                    else "completed"
                ),
                "phase": game.phase,
                "round_start": {
                    "event_id": game.round_start_event_id,
                    "round": game.round_number + 1 if game.phase == "COUNTDOWN" else game.round_number,
                    "prompt": game.prompt if game.phase == "DRAWING" and now >= (game.round_started_at or 0) else None,
                } if game.round_start_event_id and game.phase in ("COUNTDOWN", "DRAWING") else None,
                "players": [
                    player.name
                    for player in sorted(
                        game.players.values(),
                        key=lambda player: player.seat,
                    )
                ],
                "player_emojis": [player.emoji for player in sorted(game.players.values(), key=lambda player: player.seat)],
                "round_winners": [
                    {**result, "drawings": [
                        {**player, "is_winner": player["id"] == result.get("winner_id"),
                         "drawing_url": f"/round-drawings/{key}" if key in self.round_drawings else None}
                        for player in result.get("drawing_players", [])
                        for key in [result["event_id"] if player["id"] == result.get("winner_id")
                                    else f"{result['event_id']}:{player['id']}"]
                    ], "drawing_url": (
                        f"/round-drawings/{result['event_id']}"
                        if result["event_id"] in self.round_drawings else None
                    )}
                    for result in game.round_winners
                    if self.round_result_visible(result, game.paused_at if game.paused_phase == "ROUND_RESULT" else now)
                ],
                "game_result": self.game_result(game) if all(
                    self.round_result_visible(result, game.paused_at if game.paused_phase == "ROUND_RESULT" else now) for result in game.round_winners
                ) else None,
            }
            for game in self.games.values()
            if game.players
            and (
                game.phase != "FINAL_RESULT"
                or game.created_at >= self.dashboard_reset_at
            )
        ]

        return {
            "players": players,
            "games": games,
            "reset_at": self.dashboard_reset_at,
        }

    @staticmethod
    def round_result_visible(result: dict, now: float) -> bool:
        if not result.get("winner"):
            return True
        # Keep the decision private until cosmetic drawing has finished. Allow
        # the final upload to arrive; a missing image must not hide scores forever.
        return now >= result.get("reveal_after", 0) and (
            result.get("drawing_received", False) or now >= result.get("drawing_deadline", 0)
        )

    @staticmethod
    def game_result(game: Game) -> dict | None:
        if game.phase != "FINAL_RESULT" or not game.players:
            return None
        top_score = max(player.score for player in game.players.values())
        winners = [player.name for player in game.players.values() if player.score == top_score]
        return {
            "event_id": f"{game.id}-final-{game.transition_token}",
            "winners": winners,
            "score": top_score,
            "tied": len(winners) > 1,
            "artwork_id": game.artwork_id,
            "global_rankings": game.global_rankings,
            "leaderboard_save_status": game.leaderboard_save_status,
            "players": [{"id": player.id, "name": player.name, "score": player.score, "emoji": player.emoji}
                        for player in sorted(game.players.values(), key=lambda player: player.seat)],
            "rounds": [{"round": result["round"], "winner": result["winner"],
                        "winner_id": result.get("winner_id"),
                        "points": result["points"],
                        "base_points": result.get("base_points", 1000 if result["winner"] else 0),
                        "time_bonus": result.get("time_bonus", max(0, result["points"] - 1000))}
                       for result in game.round_winners],
        }

    def reset_dashboard(self) -> dict[str, Any]:
        gateway = getattr(self, 'leaderboard_gateway', None)
        if gateway:
            gateway.reset()
            self.apply_dashboard_reset()
            return self.leaderboard()
        self.round_drawings.clear()
        self.artworks.clear()
        with closing(sqlite3.connect(self.leaderboard_path)) as connection, connection:
            connection.execute("DELETE FROM leaderboard")

        self.dashboard_reset_at = time.time()
        return self.leaderboard()

    def capture_artwork(self, game: Game, complete=False):
        self.artworks.capture(game.artwork_id,
                              [p.name for p in sorted(game.players.values(), key=lambda p: p.seat)],
                              game.round_winners, complete=complete,
                              emojis=[p.emoji for p in sorted(game.players.values(), key=lambda p: p.seat)])

    @staticmethod
    def clean_name(name: str) -> str:
        cleaned_name = " ".join(
            name.strip().split()
        )

        if not cleaned_name:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Please enter your name."
                ),
            )

        return cleaned_name[:24]

    def admin_state(self) -> dict[str, Any]:
        classes = self.classes_by_model[self.model_key]
        return {
            "model_key": self.model_key,
            "round_seconds": self.round_seconds,
            "cosmetic_draw_seconds_by_round": list(self.cosmetic_draw_seconds_by_round),
            "prediction_gap_seconds": self.prediction_gap_seconds,
            "required_hits": self.required_hits,
            "confidence_threshold": self.confidence_threshold,
            "classes": [
                {
                    "name": class_name,
                    "enabled": class_name not in self.excluded_classes,
                    "difficulty": self.difficulties_by_model[self.model_key][class_name],
                }
                for class_name in classes
            ],
        }

    def update_admin_settings(
        self, *args, **kwargs,
    ) -> dict[str, Any]:
        with self.admin_settings_lock:
            self.ensure_admin_settings()
            return self._update_admin_settings(*args, **kwargs)

    def _update_admin_settings(
        self,
        model_key: str,
        round_seconds: int,
        excluded_classes: list[str],
        class_difficulties: dict[str, str] | None = None,
        required_hits: int = REQUIRED_CONSECUTIVE_HITS,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
        prediction_gap_seconds: float = PREDICTION_GAP_SECONDS,
        cosmetic_draw_seconds_by_round: list[float] | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        if cosmetic_draw_seconds_by_round is not None and (
            len(cosmetic_draw_seconds_by_round) != MAX_ROUNDS or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 10
                for value in cosmetic_draw_seconds_by_round
            )
        ):
            raise HTTPException(400, "Set a cosmetic drawing duration from 0 to 10 seconds for each of the six rounds.")
        if not 0.1 <= prediction_gap_seconds <= 5.0:
            raise HTTPException(status_code=400, detail="Guess gap must be between 0.1 and 5 seconds.")
        if type(required_hits) is not int or not 1 <= required_hits <= 10:
            raise HTTPException(status_code=400, detail="Required stable guesses must be an integer from 1 to 10.")
        if not 0.01 <= confidence_threshold <= 1.0:
            raise HTTPException(status_code=400, detail="Pass confidence must be between 1% and 100%.")
        classes = self.classes_by_model.get(model_key)

        if classes is None:
            raise HTTPException(
                status_code=400,
                detail="Please choose a supported AI model.",
            )

        unknown_classes = set(excluded_classes) - set(classes)
        if unknown_classes:
            raise HTTPException(
                status_code=400,
                detail="One or more excluded classes are not in the selected model.",
            )

        if len(unknown_classes) == len(classes):
            raise HTTPException(
                status_code=400,
                detail="At least one class must remain enabled.",
            )

        if len(set(excluded_classes)) >= len(classes):
            raise HTTPException(
                status_code=400,
                detail="At least one class must remain enabled.",
            )

        difficulties = (self.difficulties_by_model[model_key].copy()
                        if class_difficulties is None else class_difficulties.copy())
        if set(difficulties) != set(classes) or any(
            level not in LEVELS for level in difficulties.values()
        ):
            raise HTTPException(status_code=400, detail="Assign every class to easy, medium, or hard.")
        for level in LEVELS:
            if sum(name not in excluded_classes and difficulties[name] == level
                   for name in classes) < 2:
                raise HTTPException(status_code=400, detail=f"Enable at least two {level} classes for six unique rounds.")

        gateway = getattr(self, 'leaderboard_gateway', None)
        if persist and gateway:
            settings = dict(model_key=model_key, round_seconds=round_seconds, excluded_classes=excluded_classes,
                class_difficulties=difficulties, required_hits=required_hits, confidence_threshold=confidence_threshold,
                prediction_gap_seconds=prediction_gap_seconds,
                cosmetic_draw_seconds_by_round=list(cosmetic_draw_seconds_by_round if cosmetic_draw_seconds_by_round is not None else self.cosmetic_draw_seconds_by_round))
            result = gateway.request('PUT', '/game-data/settings', {'settings': settings})
            if result.get('status') != 'saved':
                raise LeaderboardUnavailable('Admin settings could not be confirmed as saved. Please retry.')
        self.difficulties_by_model[model_key] = difficulties
        self.model_key = model_key
        self.round_seconds = round_seconds
        if cosmetic_draw_seconds_by_round is not None:
            self.cosmetic_draw_seconds_by_round = list(cosmetic_draw_seconds_by_round)
        self.prediction_gap_seconds = prediction_gap_seconds
        self.required_hits = required_hits
        self.confidence_threshold = confidence_threshold
        self.excluded_classes = set(excluded_classes)
        return self.admin_state()

    @staticmethod
    def cosmetic_draw_seconds(game: Game) -> float:
        return game.cosmetic_draw_seconds_by_round[max(0, min(game.round_number - 1, MAX_ROUNDS - 1))]

    @staticmethod
    def create_game_id() -> str:
        return secrets.token_urlsafe(12)

    @staticmethod
    def create_player_id() -> str:
        return secrets.token_urlsafe(18)

    def get_game(
        self,
        game_id: str,
    ) -> Game:
        game = self.games.get(game_id)

        if game is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "Game was not found. "
                    "Please join a new game."
                ),
            )

        return game

    @staticmethod
    def get_player(
        game: Game,
        player_id: str,
    ) -> Player:
        player = game.players.get(
            player_id
        )

        if player is None:
            raise HTTPException(
                status_code=403,
                detail=(
                    "This player is not part "
                    "of the game."
                ),
            )

        return player

    @staticmethod
    def reset_player_round(
        player: Player,
    ):
        player.last_guess = ""
        player.confidence = 0.0

        player.predictions_seen = 0
        player.consecutive_hits = 0

        player.recognized_at = None

    def public_state(
        self,
        game: Game,
    ) -> dict[str, Any]:
        ordered_players = sorted(
            game.players.values(),
            key=lambda player:
            player.seat,
        )

        round_winner = (
            game.players.get(
                game.round_winner_id or ""
            )
        )

        return {
            "id": game.id,
            "cosmetic_draw_seconds": self.cosmetic_draw_seconds(game),
            "cosmetic_draw_ends_at": game.round_winners[-1]["reveal_after"] if game.phase == "ROUND_RESULT" and game.round_winners else None,
            "artwork_id": game.artwork_id if game.phase == "FINAL_RESULT" else None,
            "global_rankings": game.global_rankings if game.phase == "FINAL_RESULT" else {},
            "leaderboard_save_status": game.leaderboard_save_status,
            "round_event_id": game.round_winners[-1]["event_id"] if game.phase == "ROUND_RESULT" and game.round_winners else None,
            "snapshot_seq": time.monotonic_ns(),
            "server_time": time.time(),
            "model_key": game.model_key,
            "phase": game.phase,
            "resume_phase": game.paused_phase,
            "paused_remaining_seconds": game.paused_remaining_seconds,
            "reconnect_deadline": min((p.disconnected_until for p in game.players.values()
                                       if p.disconnected_until is not None), default=None),
            "cancellation_message": game.cancellation_message,

            "round_number": (
                game.round_number
            ),

            "max_rounds": game.max_rounds,
            "difficulty": round_difficulty(
                game.round_number + 1 if game.phase == "COUNTDOWN" else game.round_number
            ),
            "round_seconds": (
                game.round_seconds
            ),

            "prompt": game.prompt,

            "countdown_ends_at": (
                game.countdown_ends_at
            ),

            "round_started_at": (
                game.round_started_at
            ),

            "round_deadline": (
                game.round_deadline
            ),

            "round_winner_id": (
                game.round_winner_id
            ),

            "round_winner_name": (
                round_winner.name
                if round_winner
                else None
            ),

            "round_points": (
                game.round_points
            ),

            "players": [
                {
                    "id": player.id,
                    "name": player.name,
                    "emoji": player.emoji,
                    "seat": player.seat,
                    "ready": player.ready,
                    "connected": player.connected,

                    "score": player.score,

                    "round_wins": (
                        player.round_wins
                    ),

                    "last_guess": (
                        player.last_guess
                    ),

                    "confidence": (
                        player.confidence
                    ),

                    "consecutive_hits": (
                        player
                        .consecutive_hits
                    ),

                    "recognized": (
                        player.recognized_at
                        is not None
                    ),
                }
                for player
                in ordered_players
            ],

            "judge": {
                "prediction_gap_seconds": game.prediction_gap_seconds,
                "confidence_threshold": (
                    game.confidence_threshold
                ),

                "required_hits": (
                    game.required_hits
                ),

                "minimum_seconds": (
                    MINIMUM_JUDGE_SECONDS
                ),
            },
        }

    async def broadcast(
        self,
        game: Game,
    ):
        payload = {
            "type": "game_state",
            "game": self.public_state(game),
        }

        async def send(connection):
            websocket, _ = connection
            try:
                await asyncio.wait_for(websocket.send_json(payload), timeout=1.0)
            except Exception:
                self.disconnect_websocket(game.id, websocket)
                try:
                    await asyncio.wait_for(websocket.close(code=1013), timeout=0.25)
                except Exception:
                    pass

        # A slow browser must not delay delivery to the other player.
        await asyncio.gather(*(send(connection) for connection in
                               list(self.connections.get(game.id, []))))

    def track_connection_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.connection_tasks.add(task)
        task.add_done_callback(self.connection_tasks.discard)
        return task

    def pause_game(self, game):
        if game.phase not in {"COUNTDOWN", "DRAWING", "ROUND_RESULT", "RESUMING"}:
            return
        if game.phase != "RESUMING":
            game.paused_phase = game.phase
            game.paused_at = time.time()
            game.paused_remaining_seconds = max(0, (game.round_deadline or game.paused_at) - game.paused_at)
        game.phase = "PAUSED"
        game.transition_token += 1
        game.countdown_ends_at = None

    async def connect_websocket(self, game, player_id, websocket):
        player = self.get_player(game, player_id)
        if player.disconnected_until is not None and time.time() >= player.disconnected_until:
            await self.leave_game(game.id, player_id, disconnected=True)
            await websocket.close(code=4403)
            return False
        lock = self.game_locks.get(game.id)
        if lock is None:
            await websocket.close(code=4403)
            return False
        async with lock:
            self.get_player(game, player_id)
            if player.disconnected_until is not None and time.time() >= player.disconnected_until:
                await websocket.close(code=4403)
                return False
            task = self.disconnect_tasks.pop((game.id, player_id), None)
            if task:
                task.cancel()
            self.connections.setdefault(game.id, []).append((websocket, player_id))
            player.connected = True
            player.disconnected_until = None
            if game.phase == "PAUSED" and len(game.players) == MAX_PLAYERS and all(p.connected for p in game.players.values()):
                game.phase = "RESUMING"
                game.countdown_ends_at = time.time() + COUNTDOWN_SECONDS
                game.transition_token += 1
                self.track_connection_task(self.resume_after_countdown(game.id, game.transition_token))
            elif game.phase in {"WAITING", "LOBBY"} and len(game.players) == MAX_PLAYERS and all(p.connected and p.ready for p in game.players.values()):
                await self.start_countdown(game)
            await self.broadcast(game)
            return True

    def disconnect_websocket(self, game_id, websocket):
        game = self.games.get(game_id)
        if game is None:
            self.connections.pop(game_id, None)
            return
        connections = self.connections.get(game_id, [])
        affected = {pid for socket, pid in connections if socket is websocket}
        self.connections[game_id] = [(socket, pid) for socket, pid in connections if socket is not websocket]
        for player_id in affected:
            if not any(pid == player_id for _, pid in self.connections.get(game_id, [])) and player_id in game.players:
                game.players[player_id].connected = False
                self.schedule_disconnect_cleanup(game_id, player_id)
                self.pause_game(game)
        if affected:
            self.track_connection_task(self.broadcast(game))

    def schedule_disconnect_cleanup(self, game_id, player_id):
        key = (game_id, player_id)
        if key in self.disconnect_tasks or game_id not in self.games:
            return
        player = self.games[game_id].players.get(player_id)
        if player is None:
            return
        player.disconnected_until = time.time() + DISCONNECT_GRACE_SECONDS

        async def expire():
            try:
                await asyncio.sleep(max(0, player.disconnected_until - time.time()) if player.disconnected_until else 0)
                if not any(pid == player_id for _, pid in self.connections.get(game_id, [])):
                    await self.leave_game(game_id, player_id, disconnected=True)
            except HTTPException:
                pass
            finally:
                if self.disconnect_tasks.get(key) is asyncio.current_task():
                    self.disconnect_tasks.pop(key, None)

        self.disconnect_tasks[key] = asyncio.create_task(expire())

    async def resume_after_countdown(self, game_id, token):
        game = self.games.get(game_id)
        if game is None:
            return
        await asyncio.sleep(max(0, (game.countdown_ends_at or time.time()) - time.time()))
        lock = self.game_locks.get(game_id)
        if lock is None:
            return
        async with lock:
            if game.phase != "RESUMING" or game.transition_token != token:
                return
            if not all(p.connected for p in game.players.values()):
                self.pause_game(game)
                return
            delta = time.time() - (game.paused_at or time.time())
            phase = game.paused_phase
            game.phase = phase
            game.countdown_ends_at = None
            if game.round_started_at is not None:
                game.round_started_at += delta
            if game.round_deadline is not None:
                game.round_deadline += delta
            if game.result_transition_at is not None:
                game.result_transition_at += delta
            if phase == "ROUND_RESULT" and game.round_winners:
                for field_name in ("reveal_after", "drawing_deadline"):
                    game.round_winners[-1][field_name] += delta
            game.paused_phase = None
            game.paused_at = None
            game.paused_remaining_seconds = None
            game.transition_token += 1
            token = game.transition_token
            if phase == "COUNTDOWN":
                game.countdown_ends_at = time.time() + PROMPT_DELIVERY_LEAD_SECONDS
                self.track_connection_task(self.begin_round_after_countdown(game.id, token))
            elif phase == "DRAWING":
                self.track_connection_task(self.end_round_on_timeout(game.id, token))
            elif phase == "ROUND_RESULT":
                self.track_connection_task(self.advance_after_result(game.id, token))
            await self.broadcast(game)

    def prune_queue(self):
        now = time.time()
        for key, entry in list(self.waiting_queue.items()):
            if now - entry.last_seen >= QUEUE_TTL_SECONDS:
                self.waiting_queue.pop(key, None)

    def occupied_seats(self):
        return sum(len(game.players) for game in self.games.values())

    async def promote_queue_locked(self):
        self.prune_queue()
        while self.waiting_queue and self.occupied_seats() < MAX_PLAYERS:
            key, entry = next(iter(self.waiting_queue.items()))
            try:
                game, player = await self.seat_player_locked(entry.name, entry.model_key, entry.client_id)
            except LeaderboardUnavailable:
                # Releasing an old seat must succeed even if storage is offline.
                return
            self.waiting_queue.pop(key, None)
            self.queue_assignments[key] = (game.id, player.id)

    async def join_matchmaking(self, name, model_key=None, client_id=None):
        await asyncio.to_thread(self.ensure_admin_settings)
        player_name = self.clean_name(name)
        model_key = model_key or self.model_key
        if model_key not in self.classes_by_model:
            raise HTTPException(400, "Please choose a supported AI model.")
        async with self.matchmaking_lock:
            self.prune_queue()
            for existing in self.games.values():
                for occupant in existing.players.values():
                    if client_id and occupant.client_id == client_id:
                        return existing, occupant
                    if occupant.name.casefold() == player_name.casefold():
                        raise HTTPException(409, "This username already has an active session. Reconnect or wait a few seconds after leaving.")
            for entry in self.waiting_queue.values():
                if client_id and entry.client_id == client_id:
                    entry.last_seen = time.time()
                    return None, entry
                if entry.name.casefold() == player_name.casefold():
                    raise HTTPException(409, "This username is already waiting in the queue.")
            if getattr(self, 'leaderboard_gateway', None):
                # Resolve once at entry, so later queue promotion uses its cache.
                await asyncio.to_thread(self.player_emoji, player_name)
            await self.promote_queue_locked()
            if self.occupied_seats() >= MAX_PLAYERS or self.waiting_queue:
                if len(self.waiting_queue) >= MAX_QUEUE_SIZE:
                    raise HTTPException(409, "The waiting queue is full. Please try again later.")
                entry = QueueEntry(secrets.token_urlsafe(24), player_name, model_key, client_id)
                self.waiting_queue[entry.id] = entry
                return None, entry
            return await self.seat_player_locked(player_name, model_key, client_id)

    async def queue_status(self, queue_id):
        async with self.matchmaking_lock:
            self.prune_queue()
            entry = self.waiting_queue.get(queue_id)
            if entry:
                entry.last_seen = time.time()
            await self.promote_queue_locked()
            assigned = self.queue_assignments.get(queue_id)
            if assigned:
                game = self.games.get(assigned[0])
                if game and assigned[1] in game.players:
                    return {"queued": False, "player_id": assigned[1], "game": self.public_state(game)}
                self.queue_assignments.pop(queue_id, None)
            if queue_id not in self.waiting_queue:
                raise HTTPException(404, "Your queue session has expired. Please join again.")
            return {"queued": True, "queue_id": queue_id,
                    "position": list(self.waiting_queue).index(queue_id) + 1,
                    "message": "Game in progress\u2014please wait."}

    async def cancel_queue(self, queue_id):
        async with self.matchmaking_lock:
            self.waiting_queue.pop(queue_id, None)
            assigned = self.queue_assignments.pop(queue_id, None)
        if assigned and assigned[0] in self.games and assigned[1] in self.games[assigned[0]].players:
            await self.leave_game(*assigned)
        return {"status": "left"}

    async def seat_player_locked(self, player_name, model_key, client_id):
        if self.occupied_seats() >= MAX_PLAYERS:
            raise HTTPException(409, "Game in progress\u2014please wait.")
        waiting_game = None
        emoji = await asyncio.to_thread(self.player_emoji, player_name)

        for existing_game in (
            self.games.values()
        ):
            if (
                existing_game.phase
                == "WAITING"
                and existing_game.model_key
                == model_key
                and len(
                    existing_game.players
                )
                == 1
            ):
                waiting_game = (
                    existing_game
                )
                break

        if waiting_game:
            player = Player(
                id=(
                    self
                    .create_player_id()
                ),
                name=player_name,
                seat=2,
                client_id=client_id,
                emoji=emoji,
            )

            waiting_game.players[
                player.id
            ] = player

            waiting_game.phase = "LOBBY"
            self.schedule_disconnect_cleanup(waiting_game.id, player.id)

            await self.broadcast(
                waiting_game
            )

            return (
                waiting_game,
                player,
            )

        game = Game(
            id=self.create_game_id(),
            model_key=model_key,
            classes=[
                item
                for item in self.classes_by_model[model_key]
                if item not in self.excluded_classes
            ],
            round_seconds=self.round_seconds,
            cosmetic_draw_seconds_by_round=list(self.cosmetic_draw_seconds_by_round),
            prediction_gap_seconds=self.prediction_gap_seconds,
            required_hits=self.required_hits,
            confidence_threshold=self.confidence_threshold,
            class_difficulties=self.difficulties_by_model[model_key].copy(),
        )

        if not game.classes:
            raise HTTPException(
                status_code=400,
                detail="At least one class must remain enabled.",
            )

        player = Player(
            id=self.create_player_id(),
            name=player_name,
            seat=1,
            client_id=client_id,
            emoji=emoji,
        )

        game.players[player.id] = (
            player
        )

        self.games[game.id] = game

        self.game_locks[game.id] = (
            asyncio.Lock()
        )

        self.connections[game.id] = []
        self.schedule_disconnect_cleanup(game.id, player.id)

        return game, player


    async def mark_ready(
        self,
        game_id: str,
        player_id: str,
    ) -> dict:
        game = self.get_game(game_id)

        game_lock = self.game_locks[
            game.id
        ]

        async with game_lock:
            player = self.get_player(
                game,
                player_id,
            )

            if game.phase not in {
                "WAITING",
                "LOBBY",
            }:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The game has already "
                        "started."
                    ),
                )

            player.ready = True

            both_players_ready = (
                len(game.players)
                == MAX_PLAYERS
                and all(
                    current_player.ready
                    and current_player.connected
                    for current_player
                    in game.players.values()
                )
            )

            if both_players_ready:
                await self.start_countdown(
                    game
                )
            else:
                await self.broadcast(game)

            return self.public_state(game)

    async def start_countdown(
        self,
        game: Game,
    ):
        game.cancellation_message = ""
        # A waiting room may predate an Admin save. Apply the latest cosmetic
        # values once at match start, then keep them fixed for its six rounds.
        if game.round_number == 0:
            game.cosmetic_draw_seconds_by_round = list(self.cosmetic_draw_seconds_by_round)
            self.artworks.warm_up()
        game.transition_token += 1

        countdown_token = (
            game.transition_token
        )

        game.phase = "COUNTDOWN"
        game.round_start_event_id = secrets.token_urlsafe(12)

        game.countdown_ends_at = (
            time.time()
            + COUNTDOWN_SECONDS
        )

        game.round_winner_id = None
        game.round_points = 0

        for queue in self.leaderboard_subscribers:
            if not queue.full():
                queue.put_nowait(True)
        await self.broadcast(game)

        asyncio.create_task(
            self.begin_round_after_countdown(
                game.id,
                countdown_token,
            )
        )

    async def begin_round_after_countdown(
        self,
        game_id: str,
        countdown_token: int,
    ):
        game = self.games.get(game_id)
        if game is None:
            return
        await asyncio.sleep(max(0, (game.countdown_ends_at or time.time()) - time.time()
                                - PROMPT_DELIVERY_LEAD_SECONDS))

        game = self.games.get(game_id)

        if game is None:
            return

        game_lock = self.game_locks.get(
            game_id
        )

        if game_lock is None:
            return

        async with game_lock:
            if (
                game.transition_token
                != countdown_token
                or game.phase
                != "COUNTDOWN"
            ):
                return

            last_prompt = game.prompt

            prompt_options = [
                prompt
                for prompt in dict.fromkeys(game.classes)
                if prompt not in game.used_prompts
                and (not game.class_difficulties or
                     game.class_difficulties.get(prompt) == round_difficulty(game.round_number + 1))
            ]

            if not prompt_options:
                game.phase = "FINAL_RESULT"
                self.capture_artwork(game, complete=True)
                game.countdown_ends_at = None
                game.transition_token += 1
                self.record_leaderboard(game)
                await self.broadcast(game)
                return

            game.previous_prompt = (
                last_prompt
            )

            game.prompt = random.choice(
                prompt_options
            )
            game.used_prompts.add(game.prompt)

            game.round_number += 1
            game.phase = "DRAWING"

            # Deliver the prompt before its common reveal time, absorbing normal
            # network differences between the players without shortening play.
            game.round_started_at = max(
                game.countdown_ends_at or 0,
                time.time() + PROMPT_DELIVERY_LEAD_SECONDS,
            )
            game.countdown_ends_at = None

            game.round_deadline = (
                game.round_started_at
                + game.round_seconds
            )

            game.round_winner_id = None
            game.round_points = 0
            game.result_transition_at = None

            for player in (
                game.players.values()
            ):
                self.reset_player_round(
                    player
                )

            game.transition_token += 1

            round_token = (
                game.transition_token
            )

            await self.broadcast(game)

            asyncio.create_task(
                self.end_round_on_timeout(
                    game.id,
                    round_token,
                )
            )

    async def end_round_on_timeout(
        self,
        game_id: str,
        round_token: int,
    ):
        game = self.games.get(game_id)

        if game is None:
            return

        await asyncio.sleep(max(0, (game.round_started_at or time.time())
                                + game.round_seconds * LATE_ROUND_FRACTION - time.time()))

        game_lock = self.game_locks.get(game_id)
        if game_lock is None:
            return
        async with game_lock:
            if game.transition_token != round_token or game.phase != "DRAWING":
                return
            winner = self.highest_confidence_player(game)
            if winner is not None:
                await self.finish_round(game, winner=winner)
                return

        await asyncio.sleep(max(0, (game.round_deadline or time.time()) - time.time()))

        game = self.games.get(game_id)

        if game is None:
            return

        game_lock = self.game_locks.get(
            game_id
        )

        if game_lock is None:
            return

        async with game_lock:
            if (
                game.transition_token
                != round_token
                or game.phase
                != "DRAWING"
            ):
                return

            await self.finish_round(
                game,
                winner=self.highest_confidence_player(game),
            )

    @staticmethod
    def highest_confidence_player(game: Game) -> Player | None:
        candidates = sorted(
            (player for player in game.players.values()
             if player.predictions_seen > 0 and player.last_guess == game.prompt
             and player.confidence > 0),
            key=lambda player: player.confidence,
            reverse=True,
        )
        if not candidates:
            return None
        # Equal confidence is not a win; keep drawing until the tie breaks.
        if len(candidates) > 1 and candidates[0].confidence == candidates[1].confidence:
            return None
        return candidates[0]

    @staticmethod
    def prediction_wait_response(game: Game) -> dict | None:
        if game.phase != "DRAWING":
            return None
        elapsed = (time.time() - game.round_started_at
                   if game.round_started_at is not None else 0)
        minimum = FINAL_ROUND_DRAW_SECONDS if game.round_number == game.max_rounds else 0
        remaining = minimum - elapsed
        if remaining <= 0:
            return None
        return {
            "prediction": "",
            "confidence": 0.0,
            "accepted": False,
            "round_finished": False,
            "consecutive_hits": 0,
            "required_hits": game.required_hits,
            "retry_after_seconds": remaining,
        }

    async def record_prediction(
        self,
        game_id: str,
        player_id: str,
        prediction_result: dict,
        round_token: int | None = None,
    ) -> dict:
        game = self.get_game(game_id)

        game_lock = self.game_locks[
            game.id
        ]

        async with game_lock:
            player = self.get_player(
                game,
                player_id,
            )

            if game.phase != "DRAWING" or (round_token is not None and round_token != game.transition_token):
                return {
                    **prediction_result,
                    "accepted": False,
                    "round_finished": True,
                    "consecutive_hits": 0,
                    "required_hits": (
                        game.required_hits
                    ),
                }

            waiting = self.prediction_wait_response(game)
            if waiting is not None:
                return waiting

            player.predictions_seen += 1

            player.last_guess = (
                prediction_result[
                    "prediction"
                ]
            )

            player.confidence = (
                prediction_result[
                    "confidence"
                ]
            )

            prediction_is_correct = (
                player.last_guess
                == game.prompt
                and player.confidence
                >= game.confidence_threshold
            )

            if prediction_is_correct:
                player.consecutive_hits += 1
            else:
                player.consecutive_hits = 0

            current_time = time.time()

            elapsed_seconds = (
                current_time
                - (
                    game.round_started_at
                    or current_time
                )
            )

            prediction_accepted = (
                elapsed_seconds
                >= MINIMUM_JUDGE_SECONDS
                and player.predictions_seen
                >= game.required_hits
                and player.consecutive_hits
                >= game.required_hits
            )

            winner = player if prediction_accepted else None
            if elapsed_seconds >= game.round_seconds * LATE_ROUND_FRACTION:
                winner = self.highest_confidence_player(game)
                prediction_accepted = winner is not None and winner.id == player.id

            response = {
                **prediction_result,

                "accepted": (
                    prediction_accepted
                ),

                "consecutive_hits": (
                    player
                    .consecutive_hits
                ),

                "required_hits": (
                    game.required_hits
                ),
            }

            if winner is not None:
                await self.finish_round(
                    game,
                    winner=winner,
                )
            else:
                await self.broadcast(game)

            return response

    async def finish_round(
        self,
        game: Game,
        winner: Player | None,
    ):
        current_time = time.time()
        game.result_transition_at = current_time + max(ROUND_RESULT_SECONDS,
            self.cosmetic_draw_seconds(game) + DRAWING_UPLOAD_GRACE_SECONDS)

        game.phase = "ROUND_RESULT"

        # Invalidates the timeout task.
        game.transition_token += 1

        result_token = (
            game.transition_token
        )

        if winner:
            remaining_seconds = max(
                0,
                math.ceil(
                    (
                        game.round_deadline
                        or current_time
                    )
                    - current_time
                ),
            )

            round_points = (
                1000
                + remaining_seconds * 10
            )

            winner.score += round_points
            winner.round_wins += 1
            winner.recognized_at = (
                current_time
            )

            game.round_winner_id = (
                winner.id
            )

            game.round_points = (
                round_points
            )

        else:
            game.round_winner_id = None
            game.round_points = 0

        game.round_winners.append(
            {
                "event_id": secrets.token_urlsafe(12),
                "round": game.round_number,
                "winner": winner.name if winner else None,
                "winner_id": winner.id if winner else None,
                "drawing_players": [{"id": player.id, "name": player.name}
                                    for player in sorted(game.players.values(), key=lambda player: player.seat)],
                "prompt": game.prompt,
                "points": game.round_points,
                "base_points": 1000 if winner else 0,
                "time_bonus": remaining_seconds * 10 if winner else 0,
                "reveal_after": current_time + self.cosmetic_draw_seconds(game),
                "drawing_deadline": current_time + self.cosmetic_draw_seconds(game) + DRAWING_UPLOAD_GRACE_SECONDS,
            }
        )

        game.round_deadline = None
        self.capture_artwork(game)

        for queue in self.leaderboard_subscribers:
            if not queue.full():
                queue.put_nowait(True)
        await self.broadcast(game)

        asyncio.create_task(
            self.advance_after_result(
                game.id,
                result_token,
            )
        )

    async def attach_round_drawing(self, game_id: str, player_id: str, event_id: str, drawing: bytes):
        game = self.get_game(game_id)
        self.get_player(game, player_id)
        async with self.game_locks[game_id]:
            result = next((item for item in game.round_winners if item["event_id"] == event_id), None)
            if result is None:
                raise HTTPException(status_code=409, detail="This round is no longer available.")
            participants = result.get("drawing_players", [{"id": result.get("winner_id")}])
            if player_id not in {player["id"] for player in participants}:
                raise HTTPException(status_code=403, detail="Only participants of this round can submit a drawing.")
            key = event_id if result.get("winner_id") == player_id else f"{event_id}:{player_id}"
            if key not in self.round_drawings:
                self.round_drawings[key] = drawing
                self.artworks.attach(game.artwork_id, event_id, player_id, drawing)
                received = result.setdefault("drawing_received_players", [])
                received.append(player_id)
                result["drawing_received"] = all(player["id"] in received for player in participants)
                while len(self.round_drawings) > 64:
                    self.round_drawings.popitem(last=False)
                for queue in self.leaderboard_subscribers:
                    if not queue.full():
                        queue.put_nowait(True)
        return {"status": "ok"}

    async def advance_after_result(
        self,
        game_id: str,
        result_token: int,
    ):
        current_game = self.games.get(game_id)
        if current_game is None:
            return
        await asyncio.sleep(max(0, current_game.result_transition_at - time.time())
                            if current_game.result_transition_at is not None else
                            max(ROUND_RESULT_SECONDS, self.cosmetic_draw_seconds(current_game) + DRAWING_UPLOAD_GRACE_SECONDS))

        game = self.games.get(game_id)

        if game is None:
            return

        game_lock = self.game_locks.get(
            game_id
        )

        if game_lock is None:
            return

        async with game_lock:
            if (
                game.transition_token
                != result_token
                or game.phase
                != "ROUND_RESULT"
            ):
                return

            if (
                game.round_number
                >= game.max_rounds
            ):
                game.phase = "FINAL_RESULT"
                self.capture_artwork(game, complete=True)
                self.record_leaderboard(game)
                game.transition_token += 1

                for queue in self.leaderboard_subscribers:
                    if not queue.full():
                        queue.put_nowait(True)
                await self.broadcast(game)

                return

            await self.start_countdown(game)

    async def restart_game(
        self,
        game_id: str,
        player_id: str,
    ) -> dict:
        game = self.get_game(game_id)

        game_lock = self.game_locks[
            game.id
        ]

        async with game_lock:
            self.get_player(
                game,
                player_id,
            )

            if (
                game.phase
                != "FINAL_RESULT"
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The game has not "
                        "finished yet."
                    ),
                )

            game.transition_token += 1

            self.capture_artwork(game, complete=True)
            game.artwork_id = secrets.token_urlsafe(24)
            game.phase = "LOBBY"
            game.cosmetic_draw_seconds_by_round = list(self.cosmetic_draw_seconds_by_round)
            game.round_number = 0

            game.prompt = ""
            game.previous_prompt = ""
            game.used_prompts.clear()
            game.leaderboard_recorded = False
            game.leaderboard_save_status = 'not_saved'
            game.global_rankings = {}

            game.countdown_ends_at = None
            game.round_started_at = None
            game.round_deadline = None

            game.round_winner_id = None
            game.round_points = 0
            game.round_winners = []

            for player in (
                game.players.values()
            ):
                player.ready = False
                player.score = 0
                player.round_wins = 0

                self.reset_player_round(
                    player
                )

            await self.broadcast(game)

            return self.public_state(game)

    async def leave_game(
        self,
        game_id: str,
        player_id: str,
        disconnected: bool = False,
    ):
        game = self.get_game(game_id)

        game_lock = self.game_locks[
            game.id
        ]

        async with self.matchmaking_lock:
            async with game_lock:
                self.get_player(
                    game,
                    player_id,
                )

                completed = game.phase == "FINAL_RESULT"
                if completed:
                    self.capture_artwork(game, complete=True)
                game.players.pop(
                    player_id,
                    None,
                )
                for key, assignment in list(self.queue_assignments.items()):
                    if assignment == (game_id, player_id):
                        self.queue_assignments.pop(key, None)
                task = self.disconnect_tasks.pop((game_id, player_id), None)
                if task and task is not asyncio.current_task():
                    task.cancel()
                for queue in self.leaderboard_subscribers:
                    if not queue.full():
                        queue.put_nowait(True)

                game.transition_token += 1

                # Remove WebSockets belonging
                # to the leaving player.
                leaving_sockets = [socket for socket, pid in self.connections.get(game.id, []) if pid == player_id]
                self.connections[
                    game.id
                ] = [
                    (
                        websocket,
                        connected_player_id,
                    )
                    for (
                        websocket,
                        connected_player_id,
                    )
                    in self.connections.get(
                        game.id,
                        [],
                    )
                    if (
                        connected_player_id
                        != player_id
                    )
                ]
                async def close_leaving(socket):
                    try:
                        await asyncio.wait_for(socket.close(code=4403), timeout=0.25)
                    except Exception:
                        pass
                await asyncio.gather(*(close_leaving(socket) for socket in leaving_sockets))

                if not game.players:
                    self.games.pop(
                        game.id,
                        None,
                    )

                    self.game_locks.pop(
                        game.id,
                        None,
                    )

                    self.connections.pop(
                        game.id,
                        None,
                    )
                    await self.promote_queue_locked()
                    return

                remaining_player = next(
                    iter(
                        game.players.values()
                    )
                )

                remaining_player.seat = 1
                remaining_player.ready = False
                remaining_player.score = 0
                remaining_player.round_wins = 0

                self.reset_player_round(
                    remaining_player
                )

                game.artwork_id = secrets.token_urlsafe(24)
                game.phase = "WAITING"
                game.paused_phase = None
                game.paused_at = None
                game.paused_remaining_seconds = None
                game.result_transition_at = None
                game.global_rankings = {}
                game.cancellation_message = ("The match was cancelled because your opponent did not reconnect. Waiting for an opponent."
                                             if disconnected and not completed else "Your opponent left. Waiting for an opponent.")
                game.round_number = 0

                game.prompt = ""
                game.previous_prompt = ""
                game.used_prompts.clear()
                game.round_winners = []
                game.leaderboard_recorded = False
                game.leaderboard_save_status = 'not_saved'

                game.countdown_ends_at = None
                game.round_started_at = None
                game.round_deadline = None

                game.round_winner_id = None
                game.round_points = 0

                await self.broadcast(game)
                await self.promote_queue_locked()
