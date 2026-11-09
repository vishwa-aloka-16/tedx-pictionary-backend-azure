import asyncio
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import urllib.error

from backend.game_manager import Game, Player, GameManager
from backend.leaderboard_gateway import LeaderboardGateway, LeaderboardUnavailable
from backend.tools.import_leaderboard import read_sqlite

KEY = 'test-service-key-longer-than-thirty-two-characters'
GENERATION = 'generation-identity-000001'


class FakeService:
    def __init__(self):
        self.generation = GENERATION
        self.matches = {}
        self.calls = []
        self.offline = False
        self.lose_ack = False
        self.lose_reset_ack = False

    def request(self, method, path, payload=None, timeout=8):
        self.calls.append((method, path, payload))
        if self.offline:
            raise LeaderboardUnavailable()
        if path == '/game-data/health':
            return {'status': 'ok', 'generation': self.generation}
        if path == '/game-data/players/resolve':
            return {'name': payload['name'], 'emoji': 'fox', 'generation': self.generation}
        if path == '/game-data/leaderboard?limit=20':
            return {'generation': self.generation, 'players': self.rankings()}
        if path == '/game-data/reset':
            if payload['generation'] == self.generation:
                self.generation = payload['reset_id']
            if self.lose_reset_ack:
                self.lose_reset_ack = False
                raise LeaderboardUnavailable()
            if self.generation != payload['reset_id']:
                raise LeaderboardUnavailable(status=409, code='generation_conflict')
            return {'status': 'reset', 'generation': self.generation}
        if path.startswith('/game-data/matches/'):
            if payload['generation'] != self.generation:
                raise LeaderboardUnavailable(status=409, code='stale_generation')
            completion = path.split('/')[-1]
            self.matches.setdefault(completion, payload)
            if self.lose_ack:
                self.lose_ack = False
                raise LeaderboardUnavailable()
            rows = {row['name']: row for row in self.rankings()}
            return {'status': 'saved', 'generation': self.generation, 'completion_id': completion,
                    'rankings': [{'name': p['name'], 'rank': rows[p['name']]['rank'], 'high_score': rows[p['name']]['score'],
                                 'new_high_score': True} for p in payload['players']]}
        raise AssertionError(path)

    def rankings(self):
        rows = {}
        for payload in self.matches.values():
            if payload['generation'] != self.generation:
                continue
            for p in payload['players']:
                old = rows.setdefault(p['name'], {'name': p['name'], 'score': 0, 'games': 0, 'wins': 0, 'emoji': 'fox'})
                old['score'] = max(old['score'], p['score'])
                old['games'] += 1
                old['wins'] += p['wins']
        ordered = sorted(rows.values(), key=lambda p: (-p['score'], p['name']))
        return [{**p, 'rank': i + 1} for i, p in enumerate(ordered)]


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'delivery.sqlite3'
        self.service = FakeService()
        self.gateway = self.create_gateway()
        self.gateway.prepare()
        self.players = [Player(id='a', name='Alice', seat=1, score=3000, round_wins=3),
                        Player(id='b', name='Bob', seat=2, score=1000, round_wins=1)]

    def create_gateway(self):
        gateway = LeaderboardGateway('https://art.example.com', KEY, self.path)
        gateway.request = self.service.request
        return gateway

    def retry_now(self):
        with self.gateway.db() as db:
            db.execute('UPDATE leaderboard_outbox SET next_attempt = 0')

    def test_enqueue_is_durable_and_does_not_call_network_during_round_completion(self):
        calls = len(self.service.calls)
        self.gateway.enqueue('completion-token-000001', self.players)
        self.gateway.enqueue('completion-token-000001', self.players)
        self.assertEqual(len(self.service.calls), calls)
        restarted = self.create_gateway()
        self.assertEqual(restarted.pending_count(), 1)
        events = restarted.flush()
        self.assertEqual(events[0]['rankings'][0]['rank'], 1)
        self.assertEqual(restarted.pending_count(), 0)
        self.assertEqual(self.service.rankings()[0]['games'], 1)
        with restarted.db() as db:
            self.assertNotIn(KEY, str(db.execute('SELECT * FROM leaderboard_delivery_state').fetchall()))

    def test_network_failure_and_lost_acknowledgement_keep_one_completion(self):
        self.gateway.enqueue('completion-token-000001', self.players)
        self.service.offline = True
        self.assertEqual(self.gateway.flush(), [])
        self.assertEqual(self.gateway.pending_count(), 1)
        self.service.offline = False
        self.service.lose_ack = True
        self.retry_now()
        self.assertEqual(self.gateway.flush(), [])
        self.assertEqual(len(self.service.matches), 1)
        self.retry_now()
        self.assertEqual(self.gateway.flush()[0]['type'], 'saved')
        self.assertEqual(self.service.rankings()[0]['games'], 1)

    def test_reset_ack_loss_survives_restart_and_old_pending_scores_are_not_restored(self):
        self.gateway.enqueue('completion-token-000001', self.players)
        self.service.lose_reset_ack = True
        with self.assertRaises(LeaderboardUnavailable):
            self.gateway.reset()
        restarted = self.create_gateway()
        events = restarted.flush()
        self.assertEqual(events, [{'type': 'reset'}])
        self.assertEqual(restarted.pending_count(), 0)
        self.assertEqual(self.service.rankings(), [])
        restarted.enqueue('completion-token-000002', self.players)
        restarted.flush()
        self.assertEqual(self.service.rankings()[0]['score'], 3000)

    def test_stale_generation_is_discarded_but_other_conflicts_are_retained(self):
        self.gateway.enqueue('completion-token-000001', self.players)
        self.service.generation = 'different-generation-000001'
        self.assertEqual(self.gateway.flush()[0]['type'], 'discarded')
        self.gateway.generation = self.service.generation
        self.gateway.enqueue('completion-token-000002', self.players)
        original = self.gateway.request
        def conflict(method, path, payload=None, timeout=8):
            if method == 'PUT':
                raise LeaderboardUnavailable(status=409)
            return original(method, path, payload, timeout)
        self.gateway.request = conflict
        self.assertEqual(self.gateway.flush(), [])
        self.assertEqual(self.gateway.pending_count(), 1)

    def test_profiles_are_cached_and_leaderboard_cache_does_not_expose_mutable_rows(self):
        self.assertEqual(self.gateway.profile('Alice'), self.gateway.profile('Alice'))
        self.assertEqual(sum(path.endswith('/resolve') for _, path, _ in self.service.calls), 1)
        self.gateway.enqueue('completion-token-000001', self.players)
        self.gateway.flush()
        row = self.gateway.rows()[0]
        row['score'] = 99999
        self.assertEqual(self.gateway.rows()[0]['score'], 3000)

    def test_late_read_response_cannot_restore_a_generation_from_before_reset(self):
        original = self.gateway.request
        def stale_read(method, path, payload=None, timeout=8):
            result = original(method, path, payload, timeout)
            if '/leaderboard?' in path:
                self.gateway.reset()
            return result
        self.gateway.request = stale_read
        with self.assertRaises(LeaderboardUnavailable):
            self.gateway.rows()
        self.assertEqual(self.gateway.generation, self.service.generation)

    def test_http_client_reuses_existing_private_key_without_leaking_error_details(self):
        gateway = LeaderboardGateway('https://art.example.com', KEY, self.path)
        with patch('backend.leaderboard_gateway.urllib.request.urlopen', return_value=io.BytesIO(b'{"status":"ok"}')) as send:
            gateway.request('GET', '/game-data/health')
        request = send.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + KEY)
        self.assertEqual(request.full_url, 'https://art.example.com/game-data/health')
        error = urllib.error.HTTPError(request.full_url, 500, 'private database credentials', {}, io.BytesIO(b'{}'))
        with patch('backend.leaderboard_gateway.urllib.request.urlopen', side_effect=error):
            with self.assertRaises(LeaderboardUnavailable) as failure:
                gateway.request('GET', '/game-data/health')
        self.assertNotIn('credentials', str(failure.exception))

    def test_read_only_import_keeps_legacy_totals_separate_from_best_score(self):
        path = Path(self.temp.name) / 'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE leaderboard (player_name TEXT, score INTEGER, wins INTEGER, games INTEGER)')
            db.executemany('INSERT INTO leaderboard VALUES (?, ?, ?, ?)', [('Alice', 1700, 1, 1), ('Bob', 9000, 5, 3)])
        before = path.read_bytes()
        result = read_sqlite(path)
        self.assertEqual([p['best_score'] for p in result['players']], [1700, None])
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_success_response_does_not_delete_pending_result(self):
        self.gateway.enqueue('completion-token-000001', self.players)
        self.gateway.request = Mock(return_value={'status': 'ok'})
        self.assertEqual(self.gateway.flush(), [])
        self.assertEqual(self.gateway.pending_count(), 1)

    def test_import_execution_does_not_modify_its_source_database(self):
        from backend.tools.import_leaderboard import main
        path = Path(self.temp.name) / 'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE leaderboard (player_name TEXT, score INTEGER, wins INTEGER, games INTEGER)')
            db.execute("INSERT INTO leaderboard VALUES ('Alice', 1700, 1, 1)")
        before = path.read_bytes()
        with patch('sys.argv', ['import_leaderboard', str(path), '--execute']), \
             patch('backend.tools.import_leaderboard.ARTWORK_SHARE_URL', 'https://art.example.com'), \
             patch('backend.tools.import_leaderboard.ARTWORK_SHARE_UPLOAD_KEY', KEY), \
             patch('backend.tools.import_leaderboard.LEADERBOARD_PATH', path), \
             patch.object(LeaderboardGateway, 'request', return_value={'status': 'imported'}), \
             patch('sys.stdout', new=io.StringIO()):
            main()
        self.assertEqual(path.read_bytes(), before)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_score_is_pending_until_remote_ack_and_player_state_then_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            service = FakeService()
            gateway = LeaderboardGateway('https://art.example.com', KEY, Path(folder) / 'pending.sqlite3')
            gateway.request = service.request
            gateway.prepare()
            with patch.object(GameManager, 'initialize_leaderboard'):
                manager = GameManager(['cat'], {'test': ['cat']})
            manager.leaderboard_gateway = gateway
            manager.broadcast = AsyncMock()
            game = Game(id='game', phase='FINAL_RESULT', players={'p': Player(id='p', name='Alice', seat=1, score=2000)})
            game.global_rankings = {'p': {'rank': 99}}
            manager.games[game.id] = game
            manager.record_leaderboard(game)
            self.assertEqual(game.leaderboard_save_status, 'pending')
            self.assertEqual(game.global_rankings, {})
            await manager.deliver_leaderboard()
            self.assertEqual(game.leaderboard_save_status, 'saved')
            self.assertEqual(game.global_rankings['p']['rank'], 1)
            manager.broadcast.assert_awaited_once()

    async def test_prepare_entry_wakes_remote_service_outside_event_loop(self):
        import threading
        with patch.object(GameManager, 'initialize_leaderboard'):
            manager = GameManager(['cat'], {'test': ['cat']})
        loop_thread = threading.get_ident()
        def prepare():
            self.assertNotEqual(loop_thread, threading.get_ident())
        manager.leaderboard_gateway = Mock(prepare=prepare)
        self.assertEqual(await manager.prepare_entry(), {'status': 'ok'})

    async def test_queue_promotion_uses_entry_profile_cache_during_storage_outage(self):
        with tempfile.TemporaryDirectory() as folder:
            service = FakeService()
            gateway = LeaderboardGateway('https://art.example.com', KEY, Path(folder) / 'pending.sqlite3')
            gateway.request = service.request
            with patch.object(GameManager, 'initialize_leaderboard'):
                manager = GameManager(['cat'], {'test': ['cat']})
            manager.leaderboard_gateway = gateway
            manager.broadcast = AsyncMock()
            manager.schedule_disconnect_cleanup = Mock()
            game, first = await manager.join_matchmaking('Alice', client_id='client-a')
            await manager.join_matchmaking('Bob', client_id='client-b')
            _, ticket = await manager.join_matchmaking('Carol', client_id='client-c')
            self.assertIn('Carol', gateway.profiles)
            service.offline = True
            await manager.leave_game(game.id, first.id)
            self.assertFalse((await manager.queue_status(ticket.id))['queued'])
            self.assertEqual(manager.occupied_seats(), 2)


if __name__ == '__main__':
    unittest.main()
