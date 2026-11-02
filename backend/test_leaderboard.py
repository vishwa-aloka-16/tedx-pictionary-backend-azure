import sqlite3
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from backend.game_manager import GameManager, Game, Player, PLAYER_EMOJIS


class LeaderboardHighScoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = GameManager.__new__(GameManager)
        self.manager.leaderboard_path = Path(self.temp.name) / 'scores.db'
        self.manager.initialize_leaderboard()

    def record(self, name, score):
        game = Game(id='test', players={'p': Player(id='p', name=name, seat=0, score=score)})
        self.manager.record_leaderboard(game)
        self.manager.record_leaderboard(game)
        return game

    def test_player_emoji_is_random_once_and_survives_replays_and_restart(self):
        with patch('backend.game_manager.random.choice', return_value='fox') as choose:
            first = self.manager.player_emoji('Vishwa')
            self.assertEqual(first, 'fox')
            self.assertEqual(self.manager.player_emoji('Vishwa'), first)
            self.record('Vishwa', 3000)
            self.record('Vishwa', 2000)
            self.manager.initialize_leaderboard()
            self.assertEqual(self.manager.leaderboard_rows()[0]['emoji'], first)
            choose.assert_called_once()

    def test_distinct_usernames_always_receive_unused_emojis(self):
        assigned = [self.manager.player_emoji(f'Player {index}') for index in range(50)]
        self.assertEqual(len(set(assigned)), 50)
        self.assertEqual(self.manager.player_emoji('Player 0'), assigned[0])

    def test_existing_duplicate_emojis_are_repaired_once(self):
        with closing(sqlite3.connect(self.manager.leaderboard_path)) as db, db:
            db.execute('DROP INDEX unique_player_emoji')
            db.executemany('INSERT INTO player_profiles (player_name, emoji) VALUES (?, ?)',
                           [('First', PLAYER_EMOJIS[0]), ('Second', PLAYER_EMOJIS[0]), ('Third', PLAYER_EMOJIS[0])])
        self.manager.initialize_leaderboard()
        avatars = [self.manager.player_emoji(name) for name in ('First', 'Second', 'Third')]
        self.assertEqual(avatars[0], PLAYER_EMOJIS[0])
        self.assertEqual(len(set(avatars)), 3)
        self.manager.initialize_leaderboard()
        self.assertEqual([self.manager.player_emoji(name) for name in ('First', 'Second', 'Third')], avatars)

    def test_existing_leaderboard_players_get_one_persistent_emoji(self):
        with closing(sqlite3.connect(self.manager.leaderboard_path)) as db, db:
            db.execute("INSERT INTO leaderboard (player_name, score, wins, games, best_score) VALUES ('Existing', 1500, 1, 1, 1500)")
        with patch('backend.game_manager.random.choice', return_value='panda') as choose:
            self.manager.initialize_leaderboard()
            self.manager.initialize_leaderboard()
            self.assertEqual(self.manager.leaderboard_rows()[0]['emoji'], 'panda')
            choose.assert_called_once()

    def test_new_best_celebrates_but_equal_or_lower_scores_do_not(self):
        self.record('Leader', 5000)
        first = self.record('Vishwa', 3000)
        self.assertEqual(first.global_rankings['p'], {
            'name': 'Vishwa', 'rank': 2, 'high_score': 3000, 'new_high_score': True})
        for score in (2500, 3000):
            game = self.record('Vishwa', score)
            self.assertFalse(game.global_rankings['p']['new_high_score'])
            self.assertEqual(game.global_rankings['p']['rank'], 2)
            self.assertEqual(game.global_rankings['p']['high_score'], 3000)
        best = self.record('Vishwa', 6000)
        self.assertEqual(best.global_rankings['p']['rank'], 1)
        self.assertTrue(best.global_rankings['p']['new_high_score'])

    def test_rank_includes_users_outside_top_twenty_and_both_finishing_players(self):
        for index in range(21):
            self.record(f'Leader {index}', 5000 + index)
        game = Game(id='both', players={
            'a': Player(id='a', name='Player A', seat=0, score=2000),
            'b': Player(id='b', name='Player B', seat=1, score=1000)})
        self.manager.record_leaderboard(game)
        self.assertEqual(game.global_rankings['a']['rank'], 22)
        self.assertEqual(game.global_rankings['b']['rank'], 23)
        self.assertEqual(len(self.manager.leaderboard_rows()), 20)

    def test_only_highest_single_game_per_username_is_ranked(self):
        for name, score in [('Vishwa', 3000), ('Vishwa', 2500), ('Deelaka', 1500), ('Deelaka', 1075)]:
            self.record(name, score)
        rows = self.manager.leaderboard_rows()
        self.assertEqual([(r['player_name'], r['score']) for r in rows], [('Vishwa', 3000), ('Deelaka', 1500)])
        self.assertEqual([r['games'] for r in rows], [2, 2])
        self.record('Deelaka', 4000)
        self.assertEqual([(r['player_name'], r['score']) for r in self.manager.leaderboard_rows()], [('Deelaka', 4000), ('Vishwa', 3000)])
        self.manager.initialize_leaderboard()
        self.assertEqual(self.manager.leaderboard_rows()[0]['score'], 4000)

    def test_migration_does_not_mistake_legacy_totals_for_high_scores(self):
        with closing(sqlite3.connect(self.manager.leaderboard_path)) as db, db:
            db.execute('DROP TABLE leaderboard')
            db.execute('CREATE TABLE leaderboard (player_name TEXT PRIMARY KEY, score INTEGER, wins INTEGER, games INTEGER)')
            db.executemany('INSERT INTO leaderboard VALUES (?, ?, ?, ?)', [('Old total', 9000, 5, 3), ('Single game', 1700, 1, 1)])
        self.manager.initialize_leaderboard()
        self.assertEqual([(r['player_name'], r['score']) for r in self.manager.leaderboard_rows()], [('Single game', 1700)])
        self.record('Old total', 2000)
        self.assertEqual(self.manager.leaderboard_rows()[0]['score'], 2000)
        with closing(sqlite3.connect(self.manager.leaderboard_path)) as db, db:
            self.assertEqual(db.execute("SELECT score FROM leaderboard WHERE player_name = 'Old total'").fetchone()[0], 11000)


if __name__ == '__main__':
    unittest.main()
