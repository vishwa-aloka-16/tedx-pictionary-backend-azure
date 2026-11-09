import asyncio
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from backend.game_manager import GameManager, DISCONNECT_GRACE_SECONDS


class QueueReconnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sharing = patch('backend.artwork.ARTWORK_SHARE_URL', '')
        sharing.start()
        self.addCleanup(sharing.stop)
        with patch.object(GameManager, 'initialize_leaderboard'):
            self.manager = GameManager(['apple', 'banana', 'cat', 'car', 'bicycle', 'guitar'],
                                       {'test': ['apple', 'banana', 'cat', 'car', 'bicycle', 'guitar']})
        self.manager.broadcast = AsyncMock()
        self.manager.player_emoji = Mock(return_value='avatar')
        self.manager.record_leaderboard = Mock()

    async def asyncTearDown(self):
        tasks = list(self.manager.disconnect_tasks.values()) + list(self.manager.connection_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def pair(self):
        game, a = await self.manager.join_matchmaking('Alice', client_id='browser-a')
        other, b = await self.manager.join_matchmaking('Bob', client_id='browser-b')
        self.assertIs(game, other)
        sa, sb = AsyncMock(), AsyncMock()
        await self.manager.connect_websocket(game, a.id, sa)
        await self.manager.connect_websocket(game, b.id, sb)
        return game, a, b, sa, sb

    async def test_concurrent_joins_have_two_total_seats_and_fifo_queue(self):
        result = await asyncio.gather(*(self.manager.join_matchmaking(f'Player {i}', client_id=f'browser-{i}') for i in range(6)))
        self.assertEqual(self.manager.occupied_seats(), 2)
        self.assertEqual(len(self.manager.games), 1)
        self.assertEqual(len(self.manager.waiting_queue), 4)
        self.assertEqual([player.name for game, player in result if game is None], ['Player 2', 'Player 3', 'Player 4', 'Player 5'])

    async def test_queue_is_idempotent_and_promotes_in_order_when_seats_are_freed(self):
        game, a, b, *_ = await self.pair()
        _, c = await self.manager.join_matchmaking('Carol', client_id='browser-c')
        _, d = await self.manager.join_matchmaking('Dan', client_id='browser-d')
        _, again = await self.manager.join_matchmaking('Carol', client_id='browser-c')
        self.assertIs(again, c)
        self.assertEqual((await self.manager.queue_status(d.id))['position'], 2)
        await self.manager.leave_game(game.id, a.id)
        promoted = await self.manager.queue_status(c.id)
        self.assertFalse(promoted['queued'])
        self.assertEqual(promoted['game']['id'], game.id)
        self.assertEqual((await self.manager.queue_status(d.id))['position'], 1)
        self.assertEqual(self.manager.occupied_seats(), 2)
        await self.manager.leave_game(game.id, b.id)
        self.assertFalse((await self.manager.queue_status(d.id))['queued'])
        self.assertEqual(self.manager.occupied_seats(), 2)

    async def test_queue_cancel_handles_promotion_race_without_leaking_seat(self):
        game, a, *_ = await self.pair()
        _, queued = await self.manager.join_matchmaking('Carol', client_id='browser-c')
        await self.manager.leave_game(game.id, a.id)
        self.assertIn(queued.id, self.manager.queue_assignments)
        await self.manager.cancel_queue(queued.id)
        self.assertEqual(self.manager.occupied_seats(), 1)
        self.assertNotIn(queued.id, self.manager.queue_assignments)

    async def test_queue_duplicate_names_and_expiry(self):
        await self.pair()
        _, queued = await self.manager.join_matchmaking('Carol', client_id='browser-c')
        with self.assertRaises(HTTPException):
            await self.manager.join_matchmaking('carol', client_id='another-browser')
        self.manager.waiting_queue[queued.id].last_seen -= 46
        with self.assertRaises(HTTPException) as error:
            await self.manager.queue_status(queued.id)
        self.assertEqual(error.exception.status_code, 404)

    async def test_disconnect_pauses_immediately_and_reserves_seat(self):
        game, a, b, sa, _ = await self.pair()
        now = time.time()
        game.phase = 'DRAWING'
        game.prompt = 'apple'
        game.round_number = 1
        game.round_started_at = now - 10
        game.round_deadline = now + 30
        a.score = 1200
        old_token = game.transition_token
        self.manager.disconnect_websocket(game.id, sa)
        self.assertEqual(game.phase, 'PAUSED')
        self.assertAlmostEqual(game.paused_remaining_seconds, 30, delta=.1)
        self.assertAlmostEqual(a.disconnected_until - now, DISCONNECT_GRACE_SECONDS, delta=.1)
        self.assertGreater(game.transition_token, old_token)
        self.assertEqual(a.score, 1200)
        self.assertEqual(self.manager.occupied_seats(), 2)
        self.assertFalse(self.manager.public_state(game)['players'][0]['connected'])
        result = await self.manager.record_prediction(game.id, b.id, {'prediction': 'apple', 'confidence': 1.0}, round_token=old_token)
        self.assertFalse(result['accepted'])
        self.manager.record_leaderboard.assert_not_called()

    async def test_reconnect_countdown_preserves_elapsed_time_score_and_streak(self):
        game, a, b, sa, _ = await self.pair()
        game.phase = 'DRAWING'
        game.round_number = 1
        game.prompt = 'apple'
        game.round_started_at = 90
        game.round_deadline = 130
        a.score = 1200
        a.consecutive_hits = 2
        with patch('backend.game_manager.time.time', return_value=100):
            self.manager.disconnect_websocket(game.id, sa)
        with patch('backend.game_manager.time.time', return_value=105):
            await self.manager.connect_websocket(game, a.id, AsyncMock())
        self.assertEqual(game.phase, 'RESUMING')
        self.assertEqual(game.countdown_ends_at, 108)
        with patch('backend.game_manager.time.time', return_value=108), patch('backend.game_manager.asyncio.sleep', new=AsyncMock()):
            await self.manager.resume_after_countdown(game.id, game.transition_token)
        self.assertEqual(game.phase, 'DRAWING')
        self.assertEqual((game.round_started_at, game.round_deadline), (98, 138))
        self.assertEqual((a.score, a.consecutive_hits, game.prompt), (1200, 2, 'apple'))

    async def test_disconnect_during_resume_does_not_resume_a_second_time(self):
        game, a, b, sa, sb = await self.pair()
        game.phase = 'DRAWING'
        game.round_started_at = time.time() - 5
        game.round_deadline = time.time() + 35
        self.manager.disconnect_websocket(game.id, sa)
        await self.manager.connect_websocket(game, a.id, AsyncMock())
        old_token = game.transition_token
        self.manager.disconnect_websocket(game.id, sb)
        self.assertEqual(game.phase, 'PAUSED')
        with patch('backend.game_manager.asyncio.sleep', new=AsyncMock()):
            await self.manager.resume_after_countdown(game.id, old_token)
        self.assertEqual(game.phase, 'PAUSED')

    async def test_expired_reconnect_cancels_match_and_admits_next_queued_player(self):
        game, a, b, sa, _ = await self.pair()
        _, queued = await self.manager.join_matchmaking('Carol', client_id='browser-c')
        game.phase = 'DRAWING'
        game.round_number = 2
        a.score, b.score = 1300, 1200
        with patch('backend.game_manager.DISCONNECT_GRACE_SECONDS', .02):
            self.manager.disconnect_websocket(game.id, sa)
        await asyncio.sleep(.04)
        self.assertNotIn(a.id, game.players)
        self.assertEqual(b.score, 0)
        self.assertEqual(game.phase, 'LOBBY')
        self.assertIn('cancelled', game.cancellation_message)
        self.assertFalse((await self.manager.queue_status(queued.id))['queued'])
        self.manager.record_leaderboard.assert_not_called()

    async def test_expiry_without_queue_shows_waiting_and_both_absent_removes_game(self):
        game, a, b, sa, sb = await self.pair()
        game.phase = 'DRAWING'
        with patch('backend.game_manager.DISCONNECT_GRACE_SECONDS', .02):
            self.manager.disconnect_websocket(game.id, sa)
        await asyncio.sleep(.04)
        self.assertEqual(game.phase, 'WAITING')
        self.assertIn('cancelled', game.cancellation_message)
        with patch('backend.game_manager.DISCONNECT_GRACE_SECONDS', .02):
            self.manager.disconnect_websocket(game.id, sb)
        await asyncio.sleep(.04)
        self.assertNotIn(game.id, self.manager.games)

    async def test_pause_in_round_result_preserves_reveal_and_transition_windows(self):
        game, a, b, sa, _ = await self.pair()
        game.phase = 'ROUND_RESULT'
        game.round_number = 1
        game.result_transition_at = 106
        game.round_winners = [{'winner': 'Alice', 'reveal_after': 101, 'drawing_deadline': 102}]
        with patch('backend.game_manager.time.time', return_value=100):
            self.manager.disconnect_websocket(game.id, sa)
        with patch('backend.game_manager.time.time', return_value=105):
            await self.manager.connect_websocket(game, a.id, AsyncMock())
        with patch('backend.game_manager.time.time', return_value=108), patch('backend.game_manager.asyncio.sleep', new=AsyncMock()):
            await self.manager.resume_after_countdown(game.id, game.transition_token)
        self.assertEqual(game.phase, 'ROUND_RESULT')
        self.assertEqual(game.result_transition_at, 114)
        self.assertEqual(game.round_winners[-1]['reveal_after'], 109)

    async def test_reconnect_at_expired_deadline_is_rejected_even_before_cleanup_runs(self):
        game, a, b, sa, _ = await self.pair()
        game.phase = 'DRAWING'
        self.manager.disconnect_websocket(game.id, sa)
        a.disconnected_until = time.time() - .01
        socket = AsyncMock()
        self.assertFalse(await self.manager.connect_websocket(game, a.id, socket))
        socket.close.assert_awaited_once_with(code=4403)
        self.assertNotIn(a.id, game.players)
        self.assertEqual(game.phase, 'WAITING')

    async def test_an_extra_socket_closing_does_not_pause_a_still_connected_player(self):
        game, a, _, sa, _ = await self.pair()
        await self.manager.connect_websocket(game, a.id, AsyncMock())
        game.phase = 'DRAWING'
        self.manager.disconnect_websocket(game.id, sa)
        self.assertEqual(game.phase, 'DRAWING')
        self.assertTrue(a.connected)

    async def test_queue_cannot_take_a_reserved_disconnected_seat(self):
        game, a, _, sa, _ = await self.pair()
        game.phase = 'DRAWING'
        self.manager.disconnect_websocket(game.id, sa)
        queued_game, ticket = await self.manager.join_matchmaking('Carol', client_id='browser-c')
        self.assertIsNone(queued_game)
        self.assertTrue((await self.manager.queue_status(ticket.id))['queued'])
        self.assertIn(a.id, game.players)

    async def test_http_queue_contract_promotes_without_a_third_game_seat(self):
        from backend import app as api
        from backend.test_admin_auth import request
        with patch.object(api, 'game_manager', self.manager):
            replies = []
            for index in range(3):
                status, body, _ = await request(api.app, 'POST', '/matchmaking/join',
                    {'name': f'QA {index}', 'client_id': f'qa-browser-identity-{index}'})
                self.assertEqual(status, 200)
                replies.append(body)
            self.assertTrue(replies[2]['queued'])
            self.assertEqual(replies[2]['position'], 1)
            status, queued, _ = await request(api.app, 'GET', '/matchmaking/queue/' + replies[2]['queue_id'])
            self.assertEqual(status, 200)
            self.assertTrue(queued['queued'])
            await request(api.app, 'POST', f"/games/{replies[0]['game']['id']}/leave", {'player_id': replies[0]['player_id']})
            status, promoted, _ = await request(api.app, 'GET', '/matchmaking/queue/' + replies[2]['queue_id'])
            self.assertEqual(status, 200)
            self.assertFalse(promoted['queued'])
            self.assertEqual(len(promoted['game']['players']), 2)

    async def test_resume_timer_runs_automatically_without_changing_round(self):
        game, a, _, sa, _ = await self.pair()
        game.phase = 'DRAWING'
        game.round_number = 2
        game.prompt = 'cat'
        game.round_started_at = time.time() - 5
        game.round_deadline = time.time() + 35
        old_deadline = game.round_deadline
        a.score = 1300
        self.manager.disconnect_websocket(game.id, sa)
        await asyncio.sleep(.01)
        with patch('backend.game_manager.COUNTDOWN_SECONDS', .02):
            await self.manager.connect_websocket(game, a.id, AsyncMock())
        await asyncio.sleep(.04)
        self.assertEqual(game.phase, 'DRAWING')
        self.assertEqual((game.prompt, game.round_number, a.score), ('cat', 2, 1300))
        self.assertGreater(game.round_deadline, old_deadline + .02)

    async def test_old_countdown_task_cannot_consume_a_target_while_paused(self):
        game, a, _, sa, _ = await self.pair()
        game.phase = 'COUNTDOWN'
        game.countdown_ends_at = time.time()
        token = game.transition_token
        self.manager.disconnect_websocket(game.id, sa)
        await self.manager.begin_round_after_countdown(game.id, token)
        self.assertEqual(game.phase, 'PAUSED')
        self.assertEqual(game.round_number, 0)
        self.assertEqual(game.used_prompts, set())


if __name__ == '__main__':
    unittest.main()
