import copy
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from backend.game_manager import GameManager
from backend.leaderboard_gateway import LeaderboardUnavailable


class SettingsStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved = None
        self.offline = False
        self.calls = []

    def transport(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path))
        if self.offline:
            raise LeaderboardUnavailable()
        if method == 'GET':
            return {'settings': copy.deepcopy(self.saved)}
        self.saved = copy.deepcopy(payload['settings'])
        return {'status': 'saved'}

    def manager(self):
        with patch.object(GameManager, 'initialize_leaderboard'), patch('backend.artwork.ARTWORK_SHARE_URL', ''):
            manager = GameManager(list('abcdef'), {'test': list('abcdef')})
        manager.leaderboard_gateway = Mock(request=Mock(side_effect=self.transport))
        return manager

    def config(self):
        return dict(model_key='test', round_seconds=55, excluded_classes=[], required_hits=4,
                    confidence_threshold=.7, prediction_gap_seconds=.4,
                    cosmetic_draw_seconds_by_round=[1, 2, 3, 4, 5, 6],
                    class_difficulties=dict(zip('abcdef', ['easy', 'easy', 'medium', 'medium', 'hard', 'hard'])))

    def test_all_settings_restore_after_backend_restart_without_writing_defaults(self):
        first = self.manager()
        first.update_admin_settings(**self.config())
        expected = first.admin_state()
        self.calls.clear()
        restarted = self.manager()
        restarted.ensure_admin_settings()
        self.assertEqual(restarted.admin_state(), expected)
        self.assertEqual(self.calls, [('GET', '/game-data/settings')])

    def test_failed_save_does_not_apply_unsaved_settings(self):
        manager = self.manager()
        manager.ensure_admin_settings()
        previous = manager.admin_state()
        self.offline = True
        with self.assertRaises(LeaderboardUnavailable):
            manager.update_admin_settings(**self.config())
        self.assertEqual(manager.admin_state(), previous)

    async def test_storage_outage_blocks_new_games_until_restored(self):
        manager = self.manager()
        self.offline = True
        with self.assertRaises(LeaderboardUnavailable):
            await manager.join_matchmaking('Alice')
        self.assertFalse(manager.games)
        self.assertFalse(manager.admin_settings_loaded)
        self.offline = False
        self.saved = self.config()
        manager.ensure_admin_settings()
        self.assertEqual(manager.round_seconds, 55)

    def test_invalid_stored_configuration_never_applies_partial_settings(self):
        manager = self.manager()
        self.saved = {**self.config(), 'class_difficulties': {'a': 'easy'}}
        previous = manager.admin_state()
        with self.assertRaises(LeaderboardUnavailable):
            manager.ensure_admin_settings()
        self.assertEqual(manager.admin_state(), previous)
        self.assertFalse(manager.admin_settings_loaded)

    def test_invalid_admin_update_never_reaches_storage(self):
        manager = self.manager()
        manager.ensure_admin_settings()
        self.calls.clear()
        with self.assertRaises(HTTPException):
            manager.update_admin_settings(**{**self.config(), 'required_hits': 99})
        self.assertFalse(self.calls)

    def test_hidden_cosmetic_settings_are_preserved_when_not_in_admin_request(self):
        manager = self.manager()
        manager.update_admin_settings(**self.config())
        config = self.config()
        config.pop('cosmetic_draw_seconds_by_round')
        config['round_seconds'] = 60
        manager.update_admin_settings(**config)
        self.assertEqual(self.saved['cosmetic_draw_seconds_by_round'], [1, 2, 3, 4, 5, 6])
