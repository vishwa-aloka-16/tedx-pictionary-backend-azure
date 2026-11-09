"""Read-only SQLite export; optional upload to the existing Node/Mongo service.

python -m backend.tools.import_leaderboard PATH [--execute]
Stop gameplay during import; the remote leaderboard must be empty.
"""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

from backend.config import ARTWORK_SHARE_URL, ARTWORK_SHARE_UPLOAD_KEY, LEADERBOARD_PATH
from backend.leaderboard_gateway import LeaderboardGateway, LeaderboardUnavailable


def read_sqlite(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        columns = {row[1] for row in db.execute('PRAGMA table_info(leaderboard)')}
        if not columns:
            raise ValueError('Source has no leaderboard table.')
        players = [{'name': row['player_name'], 'score': row['score'], 'wins': row['wins'], 'games': row['games'],
                    'best_score': row['best_score'] if 'best_score' in columns else row['score'] if row['games'] == 1 else None}
                   for row in db.execute('SELECT * FROM leaderboard ORDER BY player_name')]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        profiles = [{'name': row['player_name'], 'emoji': row['emoji']} for row in db.execute('SELECT * FROM player_profiles ORDER BY player_name')] if 'player_profiles' in tables else []
    return {'players': players, 'profiles': profiles}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        payload = read_sqlite(args.path)
        print(f"Source: {len(payload['players'])} score records and {len(payload['profiles'])} profiles.")
        if not args.execute:
            print('Dry run. No database data changed. Add --execute to upload to an empty remote leaderboard.')
            return
        if not payload['players']:
            print('No score records to import.')
            return
        gateway = LeaderboardGateway(ARTWORK_SHARE_URL, ARTWORK_SHARE_UPLOAD_KEY, LEADERBOARD_PATH, initialize_outbox=False)
        result = gateway.request('POST', '/game-data/import', payload, timeout=60)
        print(result['status'])
    except (ValueError, sqlite3.Error, LeaderboardUnavailable) as error:
        parser.exit(1, f'Import failed: {error}\n')


if __name__ == '__main__':
    main()
