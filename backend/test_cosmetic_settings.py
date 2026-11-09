import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from backend.game_manager import Game, GameManager, Player, DRAWING_UPLOAD_GRACE_SECONDS
from backend.schemas import AdminSettingsRequest


class CosmeticSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sharing = patch('backend.artwork.ARTWORK_SHARE_URL', '')
        sharing.start()
        self.addCleanup(sharing.stop)
        self.classes = ['a', 'b', 'c', 'd', 'e', 'f']
        self.difficulties = dict(zip(self.classes, ['easy', 'easy', 'medium', 'medium', 'hard', 'hard']))
        with patch.object(GameManager, 'initialize_leaderboard'):
            self.manager = GameManager(self.classes, {'test': self.classes})
        self.manager.difficulties_by_model = {'test': self.difficulties}
        self.manager.player_emoji = Mock(return_value='avatar')
        self.manager.schedule_disconnect_cleanup = Mock()
        self.manager.broadcast = AsyncMock()
        self.manager.artworks.warm_up = Mock()

    def save(self, durations):
        return self.manager.update_admin_settings('test', 40, [], self.difficulties,
                                                 cosmetic_draw_seconds_by_round=durations)

    def test_requested_defaults_are_used_for_every_round(self):
        defaults = [.5, .5, 1, 1.5, 3, 3]
        self.assertEqual(self.manager.admin_state()['cosmetic_draw_seconds_by_round'], defaults)
        self.assertEqual(Game(id='default').cosmetic_draw_seconds_by_round, defaults)

    async def test_waiting_games_and_rematches_pick_up_admin_changes(self):
        game, player = await self.manager.join_matchmaking('One')
        durations = [.2, .3, .4, .5, 1, 4]
        self.save(durations)
        with patch('backend.game_manager.asyncio.create_task', side_effect=lambda coroutine: coroutine.close()):
            await self.manager.start_countdown(game)
        self.assertEqual(game.cosmetic_draw_seconds_by_round, durations)
        game.phase = 'FINAL_RESULT'
        self.save([2] * 6)
        await self.manager.restart_game(game.id, player.id)
        self.assertEqual(game.cosmetic_draw_seconds_by_round, [2] * 6)

    async def test_qr_service_is_woken_once_at_match_start_and_again_for_rematch(self):
        game, player = await self.manager.join_matchmaking('One')
        self.manager.artworks.warm_up = Mock()
        with patch('backend.game_manager.asyncio.create_task', side_effect=lambda coroutine: coroutine.close()):
            await self.manager.start_countdown(game)
            self.manager.artworks.warm_up.assert_called_once()
            game.round_number = 1
            await self.manager.start_countdown(game)
            self.manager.artworks.warm_up.assert_called_once()
            game.phase = 'FINAL_RESULT'
            await self.manager.restart_game(game.id, player.id)
            await self.manager.start_countdown(game)
            self.assertEqual(self.manager.artworks.warm_up.call_count, 2)

    async def test_settings_are_snapshotted_for_new_games(self):
        durations = [.5, .5, .5, .5, .5, 3]
        self.assertEqual(self.save(durations)['cosmetic_draw_seconds_by_round'], durations)
        game, _ = await self.manager.join_matchmaking('One')
        self.save([2] * 6)
        durations[0] = 9
        self.assertEqual(game.cosmetic_draw_seconds_by_round, [.5, .5, .5, .5, .5, 3])
        for index, expected in enumerate(game.cosmetic_draw_seconds_by_round, start=1):
            game.round_number = index
            self.assertEqual(self.manager.public_state(game)['cosmetic_draw_seconds'], expected)

    def test_invalid_durations_are_rejected_without_changing_settings(self):
        original = self.manager.admin_state()['cosmetic_draw_seconds_by_round']
        for values in ([1] * 5, [1] * 7, [-1] * 6, [11] * 6, [float('nan')] * 6):
            with self.assertRaises(HTTPException):
                self.save(values)
            with self.assertRaises(ValidationError):
                AdminSettingsRequest(model_key='test', round_seconds=40, cosmetic_draw_seconds_by_round=values)
        self.assertEqual(self.manager.admin_state()['cosmetic_draw_seconds_by_round'], original)
        self.assertEqual(self.save([0, .5, 1, 3, 5, 10])['cosmetic_draw_seconds_by_round'], [0, .5, 1, 3, 5, 10])

    async def test_reveal_and_upload_deadlines_use_each_round_without_changing_points(self):
        for round_number, duration in [(1, .5), (6, 3)]:
            game = Game(id='game', round_number=round_number, round_deadline=110,
                        cosmetic_draw_seconds_by_round=[.5] * 5 + [3])
            winner = Player(id='one', name='One', seat=1)
            game.players = {'one': winner, 'two': Player(id='two', name='Two', seat=2)}
            with patch('backend.game_manager.time.time', return_value=100), \
                 patch('backend.game_manager.asyncio.create_task', side_effect=lambda coroutine: coroutine.close()):
                await self.manager.finish_round(game, winner)
            result = game.round_winners[-1]
            self.assertEqual(result['reveal_after'], 100 + duration)
            self.assertEqual(result['drawing_deadline'], 100 + duration + DRAWING_UPLOAD_GRACE_SECONDS)
            self.assertEqual(self.manager.public_state(game)['cosmetic_draw_ends_at'], 100 + duration)
            self.assertEqual(winner.score, 1100)

    async def test_long_final_window_finishes_before_game_completion(self):
        game = Game(id='game', round_number=6, phase='ROUND_RESULT', transition_token=1,
                    cosmetic_draw_seconds_by_round=[.5] * 5 + [10])
        self.manager.games[game.id] = game
        self.manager.game_locks[game.id] = asyncio.Lock()
        self.manager.record_leaderboard = Mock()
        with patch('backend.game_manager.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await self.manager.advance_after_result(game.id, 1)
        sleep.assert_awaited_once_with(10 + DRAWING_UPLOAD_GRACE_SECONDS)
        self.assertEqual(game.phase, 'FINAL_RESULT')

    def test_older_admin_requests_preserve_existing_cosmetic_values(self):
        self.save([.5] * 5 + [3])
        state = self.manager.update_admin_settings('test', 45, [], self.difficulties)
        self.assertEqual(state['cosmetic_draw_seconds_by_round'], [.5] * 5 + [3])
