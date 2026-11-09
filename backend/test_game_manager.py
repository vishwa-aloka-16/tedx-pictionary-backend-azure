import asyncio
import base64
import io
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch
from fastapi import HTTPException
from backend.difficulty import default_difficulties

from backend.game_manager import Game, GameManager, Player
from backend.schemas import AdminSettingsRequest
from pydantic import ValidationError
from PIL import Image
from backend.round_drawing import thumbnail_bytes


class PromptSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sharing = patch('backend.artwork.ARTWORK_SHARE_URL', '')
        sharing.start()
        self.addCleanup(sharing.stop)
        with patch.object(GameManager, "initialize_leaderboard"):
            self.manager = GameManager(
                ["cat", "dog", "tree", "sun", "car"],
                {"test": ["cat", "dog", "tree", "sun", "car"]},
            )
        self.manager.broadcast = AsyncMock()
        self.manager.player_emoji = Mock(return_value="avatar")
        self.manager.record_leaderboard = Mock()
        self.manager.end_round_on_timeout = AsyncMock()
        self.manager.start_countdown = AsyncMock()
        self.sleep = patch("backend.game_manager.asyncio.sleep", new_callable=AsyncMock)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        # Picking the first eligible topic exposes A/B/A regressions reliably.
        self.choice = patch("backend.game_manager.random.choice", side_effect=lambda items: items[0])
        self.choice.start()
        self.addCleanup(self.choice.stop)

    def create_game(self, classes):
        # Existing gameplay scenarios explicitly retain their original settings.
        game = Game(id="test-game", classes=classes, round_seconds=90,
                    required_hits=3, confidence_threshold=0.4,
                    cosmetic_draw_seconds_by_round=[4] * 6)
        game.players = {
            "one": Player(id="one", name="One", seat=1),
            "two": Player(id="two", name="Two", seat=2),
        }
        self.manager.games[game.id] = game
        self.manager.game_locks[game.id] = asyncio.Lock()
        self.manager.connections[game.id] = []
        return game

    async def play_rounds(self, game):
        prompts = []
        for _ in range(game.max_rounds):
            game.phase = "COUNTDOWN"
            await self.manager.begin_round_after_countdown(game.id, game.transition_token)
            self.assertEqual(game.phase, "DRAWING")
            prompts.append(game.prompt)
            game.phase = "ROUND_RESULT"
            await self.manager.advance_after_result(game.id, game.transition_token)
        self.assertEqual(game.phase, "FINAL_RESULT")
        return prompts

    async def test_five_rounds_never_repeat_a_topic(self):
        game = self.create_game(self.manager.classes)
        prompts = await self.play_rounds(game)
        self.assertEqual(len(prompts), 5)
        self.assertEqual(len(set(prompts)), 5)
        self.manager.record_leaderboard.assert_called_once_with(game)

    async def test_small_pools_finish_without_repeating(self):
        for classes in (["cat"], ["cat", "dog", "cat"]):
            with self.subTest(classes=classes):
                game = self.create_game(classes)
                self.assertEqual(self.manager.public_state(game)["max_rounds"], len(set(classes)))
                prompts = await self.play_rounds(game)
                self.assertEqual(set(prompts), set(classes))
                self.assertEqual(len(prompts), len(set(classes)))

    async def test_restart_resets_topic_history(self):
        game = self.create_game(self.manager.classes)
        first_match = await self.play_rounds(game)
        game.leaderboard_recorded = True
        await self.manager.restart_game(game.id, "one")
        self.assertFalse(game.used_prompts)
        self.assertFalse(game.leaderboard_recorded)
        self.assertEqual(game.round_number, 0)
        self.assertEqual(await self.play_rounds(game), first_match)

    async def test_player_leaving_resets_topic_history(self):
        game = self.create_game(self.manager.classes)
        game.used_prompts.add("cat")
        game.round_winners.append({"round": 1, "winner": "One", "points": 100})
        game.round_number = 1
        await self.manager.leave_game(game.id, "two")
        self.assertEqual(game.phase, "WAITING")
        self.assertFalse(game.used_prompts)
        self.assertFalse(game.round_winners)

    async def test_final_round_waits_three_point_five_seconds_without_counting_predictions(self):
        game = self.create_game(self.manager.classes)
        game.phase = "DRAWING"
        game.round_number = game.max_rounds
        game.round_started_at = 100.0
        game.prompt = "cat"
        with patch("backend.game_manager.time.time", return_value=103.499):
            response = await self.manager.record_prediction(
                game.id, "one", {"prediction": "cat", "confidence": 1.0})
            self.assertFalse(response["accepted"])
            self.assertGreater(response["retry_after_seconds"], 0)
            self.assertEqual(game.players["one"].predictions_seen, 0)
            self.assertEqual(game.players["one"].consecutive_hits, 0)
        with patch("backend.game_manager.time.time", return_value=103.5):
            self.assertIsNone(self.manager.prediction_wait_response(game))
            await self.manager.record_prediction(
                game.id, "one", {"prediction": "cat", "confidence": 1.0})
            self.assertEqual(game.players["one"].predictions_seen, 1)

    async def test_earlier_rounds_do_not_delay_inference(self):
        game = self.create_game(self.manager.classes)
        game.phase = "DRAWING"
        game.round_started_at = 100.0
        with patch("backend.game_manager.time.time", return_value=100.1):
            for round_number in range(1, game.max_rounds):
                game.round_number = round_number
                self.assertIsNone(self.manager.prediction_wait_response(game))

    async def test_last_twenty_percent_awards_higher_confidence_opponent(self):
        game = self.create_game(self.manager.classes)
        game.phase = "DRAWING"
        game.round_number = 1
        game.round_seconds = 100
        game.round_started_at = 100.0
        game.prompt = "cat"
        self.manager.finish_round = AsyncMock()
        with patch("backend.game_manager.time.time", return_value=179.99):
            await self.manager.record_prediction(game.id, "two", {"prediction": "cat", "confidence": 0.3})
            self.manager.finish_round.assert_not_called()
        with patch("backend.game_manager.time.time", return_value=180.0):
            response = await self.manager.record_prediction(game.id, "one", {"prediction": "cat", "confidence": 0.2})
        self.assertFalse(response["accepted"])
        self.manager.finish_round.assert_awaited_once_with(game, winner=game.players["two"])

    async def test_late_round_ignores_wrong_guesses_and_ties(self):
        game = self.create_game(self.manager.classes)
        game.prompt = "cat"
        for player in game.players.values():
            player.predictions_seen = 1
            player.last_guess = "cat"
            player.confidence = 0.2
        self.assertIsNone(self.manager.highest_confidence_player(game))
        game.players["two"].last_guess = "dog"
        game.players["two"].confidence = 0.99
        self.assertEqual(self.manager.highest_confidence_player(game).id, "one")
        game.players["one"].last_guess = "dog"
        self.assertIsNone(self.manager.highest_confidence_player(game))

    async def test_winner_notifies_leaderboard_before_player_broadcast(self):
        game = self.create_game(self.manager.classes)
        game.phase = "DRAWING"
        game.round_number = 1
        updates = asyncio.Queue(maxsize=1)
        self.manager.leaderboard_subscribers.add(updates)
        self.manager.advance_after_result = AsyncMock()

        async def check_notification(current_game):
            self.assertFalse(updates.empty())
            self.assertEqual(current_game.round_winners[-1]["winner"], "One")

        self.manager.broadcast.side_effect = check_notification
        await self.manager.finish_round(game, game.players["one"])
        event_id = game.round_winners[-1]["event_id"]
        # A slow subscriber must not block a subsequent winner notification.
        await self.manager.finish_round(game, game.players["one"])
        self.assertEqual(updates.qsize(), 1)
        self.assertNotEqual(game.round_winners[-1]["event_id"], event_id)

    async def test_timer_awards_existing_low_confidence_at_eighty_percent(self):
        game = self.create_game(self.manager.classes)
        game.phase = "DRAWING"
        game.prompt = "cat"
        game.players["one"].last_guess = "cat"
        game.players["one"].confidence = 0.15
        game.players["one"].predictions_seen = 1
        self.manager.finish_round = AsyncMock()
        await GameManager.end_round_on_timeout(self.manager, game.id, game.transition_token)
        self.manager.finish_round.assert_awaited_once_with(game, winner=game.players["one"])

    async def test_stale_countdown_does_not_consume_a_topic(self):
        game = self.create_game(self.manager.classes)
        game.phase = "COUNTDOWN"
        game.transition_token = 2
        await self.manager.begin_round_after_countdown(game.id, 1)
        self.assertFalse(game.used_prompts)
        self.assertEqual(game.round_number, 0)

    async def test_six_rounds_follow_configured_groups_after_restart(self):
        classes = ["apple", "banana", "cat", "car", "bicycle", "guitar"]
        game = self.create_game(classes)
        game.class_difficulties = default_difficulties(classes)
        # Custom admin assignments must override the suggested difficulty.
        game.class_difficulties["apple"] = "hard"
        game.class_difficulties["guitar"] = "easy"
        for _ in range(2):
            prompts = await self.play_rounds(game)
            self.assertEqual(len(set(prompts)), 6)
            self.assertEqual([game.class_difficulties[name] for name in prompts],
                             ["easy", "easy", "medium", "medium", "hard", "hard"])
            await self.manager.restart_game(game.id, "one")

    def test_prediction_gap_defaults_to_point_one_seconds(self):
        self.assertEqual(self.manager.admin_state()["prediction_gap_seconds"], 0.1)
        self.assertEqual(Game(id="default").prediction_gap_seconds, 0.1)

    async def test_admin_validation_and_new_game_snapshot(self):
        classes = ["apple", "banana", "cat", "car", "bicycle", "guitar"]
        self.manager.classes_by_model = {"test": classes}
        original = default_difficulties(classes)
        self.manager.difficulties_by_model = {"test": original.copy()}
        state = self.manager.update_admin_settings("test", 90, [], original,
                                                   required_hits=2, confidence_threshold=0.75, prediction_gap_seconds=0.8)
        self.assertEqual(state["prediction_gap_seconds"], 0.8)
        self.assertEqual(state["required_hits"], 2)
        self.assertEqual(state["confidence_threshold"], 0.75)
        game, _ = await self.manager.join_matchmaking("One")
        changed = {**original, "apple": "hard", "guitar": "easy"}
        self.manager.update_admin_settings("test", 60, [], changed)
        self.assertEqual(game.class_difficulties, original)
        self.assertEqual(game.round_seconds, 90)
        self.assertEqual(game.prediction_gap_seconds, 0.8)
        self.assertEqual(game.required_hits, 2)
        self.assertEqual(game.confidence_threshold, 0.75)
        judge = self.manager.public_state(game)["judge"]
        self.assertEqual(judge["prediction_gap_seconds"], 0.8)
        self.assertEqual(judge["required_hits"], 2)
        self.assertEqual(judge["confidence_threshold"], 0.75)
        invalid_cases = [
            ([], {**changed, "cat": "impossible"}),
            ([], {"apple": "easy"}),
            (["banana"], changed),
            (["unknown"], changed),
            ([], {**changed, "unknown": "easy"}),
        ]
        for excluded, difficulties in invalid_cases:
            with self.subTest(excluded=excluded, difficulties=difficulties):
                with self.assertRaises(HTTPException):
                    self.manager.update_admin_settings("test", 100, excluded, difficulties)
                self.assertEqual(self.manager.difficulties_by_model["test"], changed)
                self.assertEqual(self.manager.round_seconds, 60)

    async def test_configured_streak_and_confidence_control_acceptance(self):
        game = self.create_game(self.manager.classes)
        game.phase = "DRAWING"
        game.round_number = 1
        game.round_started_at = 100.0
        game.prompt = "cat"
        game.required_hits = 2
        game.confidence_threshold = 0.75
        self.manager.finish_round = AsyncMock()
        with patch("backend.game_manager.time.time", return_value=105.0):
            # Low-confidence and wrong guesses each reset a qualifying streak.
            for guess, confidence, expected_hits in [
                ("cat", 0.75, 1), ("cat", 0.74, 0),
                ("cat", 0.9, 1), ("dog", 0.99, 0), ("cat", 0.75, 1),
            ]:
                response = await self.manager.record_prediction(
                    game.id, "one", {"prediction": guess, "confidence": confidence})
                self.assertFalse(response["accepted"])
                self.assertEqual(response["consecutive_hits"], expected_hits)
                self.assertEqual(response["required_hits"], 2)
            response = await self.manager.record_prediction(
                game.id, "one", {"prediction": "cat", "confidence": 0.75})
            self.assertTrue(response["accepted"])
        self.manager.finish_round.assert_awaited_once_with(game, winner=game.players["one"])

    async def test_single_guess_can_pass_at_configured_confidence(self):
        game = self.create_game(self.manager.classes)
        game.phase = "DRAWING"
        game.round_number = 1
        game.round_started_at = 100.0
        game.prompt = "cat"
        game.required_hits = 1
        game.confidence_threshold = 0.2
        self.manager.finish_round = AsyncMock()
        with patch("backend.game_manager.time.time", return_value=105.0):
            response = await self.manager.record_prediction(
                game.id, "one", {"prediction": "cat", "confidence": 0.2})
        self.assertTrue(response["accepted"])

    def test_judge_settings_reject_invalid_values(self):
        for extra in ({"prediction_gap_seconds": 0.09}, {"prediction_gap_seconds": 5.1},
                      {"prediction_gap_seconds": float("nan")}, {"required_hits": 0}, {"required_hits": 11},
                      {"required_hits": 1.5}, {"required_hits": True},
                      {"confidence_threshold": 0}, {"confidence_threshold": 1.01},
                      {"confidence_threshold": float("nan")}):
            with self.subTest(extra=extra):
                with self.assertRaises(ValidationError):
                    AdminSettingsRequest(model_key="test", round_seconds=90, **extra)
                with self.assertRaises(HTTPException):
                    self.manager.update_admin_settings("test", 90, [], **extra)

    def test_final_round_wait_reports_custom_hit_count(self):
        game = self.create_game(self.manager.classes)
        game.phase = "DRAWING"
        game.round_number = game.max_rounds
        game.round_started_at = 100.0
        game.required_hits = 7
        with patch("backend.game_manager.time.time", return_value=101.0):
            self.assertEqual(self.manager.prediction_wait_response(game)["required_hits"], 7)

    async def test_slow_socket_does_not_delay_other_player(self):
        game = self.create_game(self.manager.classes)
        release_slow = asyncio.Event()
        fast_received = asyncio.Event()
        slow = Mock()
        fast = Mock()
        slow.send_json = AsyncMock(side_effect=lambda payload: None)

        async def send_slow(payload):
            await release_slow.wait()

        async def send_fast(payload):
            fast_received.set()

        slow.send_json.side_effect = send_slow
        fast.send_json = AsyncMock(side_effect=send_fast)
        self.manager.connections[game.id] = [(slow, "one"), (fast, "two")]
        task = asyncio.create_task(GameManager.broadcast(self.manager, game))
        try:
            await asyncio.wait_for(fast_received.wait(), timeout=0.2)
            self.assertFalse(task.done())
        finally:
            release_slow.set()
            await task

    async def test_previous_round_inference_cannot_change_new_round(self):
        game = self.create_game(self.manager.classes)
        game.phase = "DRAWING"
        game.transition_token = 4
        response = await self.manager.record_prediction(
            game.id, "one", {"prediction": "cat", "confidence": 1.0}, round_token=3)
        self.assertTrue(response["round_finished"])
        self.assertEqual(game.players["one"].predictions_seen, 0)

    def test_scheduled_prompt_cannot_be_judged_before_reveal(self):
        game = self.create_game(self.manager.classes)
        game.phase = "DRAWING"
        game.round_number = 1
        game.round_started_at = 101.0
        with patch("backend.game_manager.time.time", return_value=100.25):
            self.assertEqual(self.manager.prediction_wait_response(game)["retry_after_seconds"], 0.75)
        with patch("backend.game_manager.time.time", return_value=101.0):
            self.assertIsNone(self.manager.prediction_wait_response(game))

    async def test_prompt_is_prepared_before_shared_start_deadline(self):
        game = self.create_game(self.manager.classes)
        game.phase = "COUNTDOWN"
        game.countdown_ends_at = 103.0
        with patch("backend.game_manager.time.time", return_value=100.0):
            await self.manager.begin_round_after_countdown(game.id, game.transition_token)
        self.sleep.target.sleep.assert_any_await(2.25)
        self.assertEqual(game.round_started_at, 103.0)
        self.assertEqual(game.round_deadline, 193.0)
        self.assertTrue(game.prompt)

    async def test_final_drawing_never_changes_winner_score_or_round_state(self):
        game = self.create_game(self.manager.classes)
        game.phase = "DRAWING"
        game.round_number = 1
        game.prompt = "cat"
        self.manager.advance_after_result = AsyncMock()
        await self.manager.finish_round(game, game.players["one"])
        event_id = game.round_winners[-1]["event_id"]
        before = (game.phase, game.round_winner_id, game.round_points,
                  game.players["one"].score, game.transition_token)
        await self.manager.attach_round_drawing(game.id, "one", event_id, b"thumbnail")
        await self.manager.attach_round_drawing(game.id, "two", event_id, b"opponent")
        self.assertEqual(before, (game.phase, game.round_winner_id, game.round_points,
                                 game.players["one"].score, game.transition_token))
        self.assertEqual(self.manager.round_drawings[event_id], b"thumbnail")
        with patch("backend.game_manager.time.time", return_value=game.round_winners[-1]["reveal_after"]):
            snapshot = self.manager.leaderboard(rows=[])
        self.assertEqual(snapshot["games"][0]["round_winners"][0]["drawing_url"], f"/round-drawings/{event_id}")
        drawings = snapshot["games"][0]["round_winners"][0]["drawings"]
        self.assertEqual([drawing["is_winner"] for drawing in drawings], [True, False])
        self.assertEqual(drawings[1]["drawing_url"], f"/round-drawings/{event_id}:two")
        self.assertEqual(self.manager.round_drawings[f"{event_id}:two"], b"opponent")
        self.manager.record_leaderboard.assert_not_called()
        await self.manager.attach_round_drawing(game.id, "one", event_id, b"replacement")
        self.assertEqual(self.manager.round_drawings[event_id], b"thumbnail")

    async def test_final_announcement_waits_for_drawings_and_includes_score_math(self):
        game = self.create_game(self.manager.classes)
        game.phase = "DRAWING"
        game.round_number = game.max_rounds
        game.round_deadline = 120.2
        self.manager.advance_after_result = AsyncMock()
        with patch("backend.game_manager.time.time", return_value=100):
            await self.manager.finish_round(game, game.players["one"])
        game.phase = "FINAL_RESULT"
        with patch("backend.game_manager.time.time", return_value=104):
            self.assertIsNone(self.manager.leaderboard(rows=[])["games"][0]["game_result"])
            event = game.round_winners[-1]["event_id"]
            await self.manager.attach_round_drawing(game.id, "one", event, b"one")
            await self.manager.attach_round_drawing(game.id, "two", event, b"two")
            result = self.manager.leaderboard(rows=[])["games"][0]["game_result"]
        self.assertEqual(result["rounds"][0]["base_points"], 1000)
        self.assertEqual(result["rounds"][0]["time_bonus"], 210)
        self.assertEqual(result["score"], 1210)
        self.assertEqual([player["score"] for player in result["players"]], [1210, 0])

    async def test_only_recorded_winner_can_attach_matching_round(self):
        game = self.create_game(self.manager.classes)
        game.round_winners = [{"event_id": "event", "winner_id": "one"}]
        for player, event, status in [("two", "event", 403), ("one", "old-event", 409)]:
            with self.assertRaises(HTTPException) as result:
                await self.manager.attach_round_drawing(game.id, player, event, b"data")
            self.assertEqual(result.exception.status_code, status)
        self.assertFalse(self.manager.round_drawings)

    async def test_leaderboard_waits_for_cosmetic_window_and_final_drawing(self):
        game = self.create_game(self.manager.classes)
        game.phase = "DRAWING"
        game.round_number = 1
        self.manager.advance_after_result = AsyncMock()
        with patch("backend.game_manager.time.time", return_value=100.0):
            await self.manager.finish_round(game, game.players["one"])
            self.assertGreater(game.players["one"].score, 0)
            self.assertEqual(self.manager.leaderboard(rows=[])["games"][0]["round_winners"], [])
        event_id = game.round_winners[-1]["event_id"]
        with patch("backend.game_manager.time.time", return_value=104.0):
            self.assertEqual(self.manager.leaderboard(rows=[])["games"][0]["round_winners"], [])
            await self.manager.attach_round_drawing(game.id, "one", event_id, b"final drawing")
            self.assertEqual(self.manager.leaderboard(rows=[])["games"][0]["round_winners"], [])
            await self.manager.attach_round_drawing(game.id, "two", event_id, b"opponent drawing")
            result = self.manager.leaderboard(rows=[])["games"][0]["round_winners"][0]
            self.assertEqual(result["winner"], "One")
            self.assertEqual(result["drawing_url"], f"/round-drawings/{event_id}")

    def test_early_upload_cannot_reveal_winner_before_two_seconds(self):
        result = {"winner": "One", "reveal_after": 102, "drawing_deadline": 105, "drawing_received": True}
        self.assertFalse(self.manager.round_result_visible(result, 101.99))
        self.assertTrue(self.manager.round_result_visible(result, 102))

    def test_missing_drawing_reveals_result_after_upload_grace(self):
        result = {"winner": "One", "reveal_after": 102, "drawing_deadline": 105}
        self.assertFalse(self.manager.round_result_visible(result, 104.99))
        self.assertTrue(self.manager.round_result_visible(result, 105))

    async def test_cosmetic_image_cache_is_bounded(self):
        game = self.create_game(self.manager.classes)
        for index in range(65):
            event_id = f"event-{index}"
            game.round_winners = [{"event_id": event_id, "winner_id": "one"}]
            await self.manager.attach_round_drawing(game.id, "one", event_id, b"data")
        self.assertEqual(len(self.manager.round_drawings), 64)
        self.assertNotIn("event-0", self.manager.round_drawings)

    def test_thumbnail_validation_and_resize(self):
        image = Image.new("RGB", (700, 560), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        result = thumbnail_bytes("data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode())
        with Image.open(io.BytesIO(result)) as thumbnail:
            self.assertEqual(thumbnail.size, (350, 280))
        with self.assertRaises(HTTPException):
            thumbnail_bytes("data:image/png;base64,invalid")

    async def test_requested_production_defaults_and_excluded_prompts(self):
        metadata = json.loads((Path(__file__).parent / "models" / "metadata_keras.json").read_text())
        classes = metadata["classes"]["class_names"]
        with patch.object(GameManager, "initialize_leaderboard"):
            manager = GameManager(classes, {"tensorflow_50": classes})
        manager.player_emoji = Mock(return_value="avatar")
        state = manager.admin_state()
        self.assertEqual(state["model_key"], "tensorflow_50")
        self.assertEqual(state["round_seconds"], 40)
        self.assertEqual(state["prediction_gap_seconds"], 0.1)
        self.assertEqual(state["required_hits"], 3)
        self.assertEqual(state["confidence_threshold"], 0.6)
        self.assertEqual({item["name"] for item in state["classes"] if not item["enabled"]},
                         {"helicopter", "bus", "broccoli", "giraffe", "grapes"})
        game, _ = await manager.join_matchmaking("One")
        self.assertEqual(len(game.classes), 45)
        self.assertEqual((game.round_seconds, game.required_hits, game.confidence_threshold), (40, 3, 0.6))
        for level in ("easy", "medium", "hard"):
            self.assertGreaterEqual(sum(game.class_difficulties[name] == level for name in game.classes), 2)

    async def test_leaderboard_round_intro_event_survives_countdown_transition(self):
        game = self.create_game(self.manager.classes)
        updates = asyncio.Queue(maxsize=1)
        self.manager.leaderboard_subscribers.add(updates)
        self.manager.begin_round_after_countdown = AsyncMock()
        await GameManager.start_countdown(self.manager, game)
        event = self.manager.leaderboard(rows=[])["games"][0]["round_start"]
        self.assertFalse(updates.empty())
        self.assertEqual(event["round"], 1)
        game.phase = "DRAWING"
        game.round_number = 1
        game.prompt = "cat"
        game.round_started_at = 100
        self.assertIsNone(event["prompt"])
        with patch("backend.game_manager.time.time", return_value=99.9):
            self.assertIsNone(self.manager.leaderboard(rows=[])["games"][0]["round_start"]["prompt"])
        with patch("backend.game_manager.time.time", return_value=100):
            revealed = self.manager.leaderboard(rows=[])["games"][0]["round_start"]
            self.assertEqual(revealed, {**event, "prompt": "cat"})
        await GameManager.start_countdown(self.manager, game)
        next_event = self.manager.leaderboard(rows=[])["games"][0]["round_start"]
        self.assertEqual(next_event["round"], 2)
        self.assertNotEqual(next_event["event_id"], event["event_id"])



if __name__ == "__main__":
    unittest.main()
