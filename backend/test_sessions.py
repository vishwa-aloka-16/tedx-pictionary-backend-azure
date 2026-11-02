import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from backend.game_manager import GameManager


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.object(GameManager, 'initialize_leaderboard'):
            self.manager = GameManager(['cat'], {'test': ['cat']})
        self.manager.broadcast = AsyncMock()
        self.manager.player_emoji = lambda name: "\U0001f98a"
        self.manager.model_key = 'test'
        self.grace = patch('backend.game_manager.DISCONNECT_GRACE_SECONDS', 0.03)
        self.grace.start()

    async def asyncTearDown(self):
        tasks = list(self.manager.disconnect_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.grace.stop()

    async def test_repeat_join_reuses_browser_identity_and_duplicate_name_cannot_fill_both_seats(self):
        game, player = await self.manager.join_matchmaking('Alice', client_id='browser-a')
        again, same = await self.manager.join_matchmaking('Alice', client_id='browser-a')
        self.assertIs(game, again)
        self.assertIs(player, same)
        with self.assertRaises(HTTPException) as error:
            await self.manager.join_matchmaking('alice', client_id='browser-b')
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(len(game.players), 1)

    async def test_empty_game_is_deleted_and_name_can_join_again(self):
        game, player = await self.manager.join_matchmaking('Alice', client_id='browser-a')
        await self.manager.leave_game(game.id, player.id)
        self.assertNotIn(game.id, self.manager.games)
        new, _ = await self.manager.join_matchmaking('Alice', client_id='browser-a')
        self.assertNotEqual(new.id, game.id)

    async def test_reconnect_within_grace_preserves_player_and_abandoned_socket_expires(self):
        game, player = await self.manager.join_matchmaking('Alice', client_id='browser-a')
        first = AsyncMock()
        await self.manager.connect_websocket(game, player.id, first)
        self.manager.disconnect_websocket(game.id, first)
        second = AsyncMock()
        await self.manager.connect_websocket(game, player.id, second)
        await asyncio.sleep(0.05)
        self.assertIn(player.id, game.players)
        self.manager.disconnect_websocket(game.id, second)
        await asyncio.sleep(0.05)
        self.assertNotIn(game.id, self.manager.games)

    async def test_join_without_socket_expires_and_no_users_means_no_game(self):
        game, _ = await self.manager.join_matchmaking('Alice', client_id='browser-a')
        await self.manager.join_matchmaking('Bob', client_id='browser-b')
        await asyncio.sleep(0.06)
        self.assertNotIn(game.id, self.manager.games)
        self.assertNotIn(game.id, self.manager.connections)

    async def test_one_disconnect_resets_game_for_remaining_active_player(self):
        game, first = await self.manager.join_matchmaking('Alice', client_id='browser-a')
        _, second = await self.manager.join_matchmaking('Bob', client_id='browser-b')
        socket = AsyncMock()
        await self.manager.connect_websocket(game, second.id, socket)
        await asyncio.sleep(0.05)
        self.assertNotIn(first.id, game.players)
        self.assertEqual(list(game.players), [second.id])
        self.assertEqual(game.phase, 'WAITING')
        self.assertEqual(second.seat, 1)


if __name__ == '__main__':
    unittest.main()
