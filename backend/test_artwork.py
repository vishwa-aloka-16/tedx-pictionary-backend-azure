import asyncio
import base64
import importlib
import io
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from PIL import Image, ImageDraw

from backend.artwork import ArtworkStore, compose_artwork, drawing_ink, artwork_signature
from backend.game_manager import PLAYER_EMOJIS
from backend.round_drawing import thumbnail_bytes
from backend.game_manager import Game, GameManager, Player


def drawing(number=0):
    image = Image.new("RGB", (350, 280), "white")
    pen = ImageDraw.Draw(image)
    if number % 3 == 0:
        pen.ellipse((70, 50, 250, 220), outline="black", width=6)
        pen.line((160, 50, 200, 15), fill="black", width=5)
    elif number % 3 == 1:
        pen.polygon([(65, 110), (175, 30), (285, 110)], outline="black", width=5)
        pen.rectangle((90, 110, 260, 245), outline="black", width=5)
        pen.rectangle((150, 170, 195, 245), outline="black", width=5)
    else:
        pen.ellipse((70, 80, 245, 185), outline="black", width=6)
        pen.polygon([(240, 133), (300, 70), (300, 205)], outline="black", width=5)
        pen.ellipse((105, 112, 117, 124), fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def rounds(count=6):
    return [{"event_id": f"event-{i}", "round": i, "drawing_deadline": 100,
             "drawing_players": [{"id": "a", "name": "Vishwa"}, {"id": "b", "name": "Amaya"}],
             "winner_id": "a" if i % 2 else None} for i in range(1, count + 1)]


class ArtworkTests(unittest.TestCase):
    def setUp(self):
        sharing = patch('backend.artwork.ARTWORK_SHARE_URL', '')
        sharing.start()
        self.addCleanup(sharing.stop)

    def test_saved_emoji_signature_is_monochrome_and_supported_for_all_avatars(self):
        glyphs = set()
        for emoji in PLAYER_EMOJIS:
            signature = artwork_signature(['Player'], [emoji])
            self.assertIsNotNone(signature.getbbox())
            self.assertEqual(signature.getchannel('R').tobytes(), signature.getchannel('G').tobytes())
            self.assertEqual(signature.getchannel('G').tobytes(), signature.getchannel('B').tobytes())
            glyphs.add(signature.tobytes())
        self.assertEqual(len(glyphs), len(PLAYER_EMOJIS))

    def test_artwork_keeps_original_players_emojis_after_a_player_leaves(self):
        store = ArtworkStore()
        store.capture('emoji', ['Vishwa', 'Amaya'], [], complete=True, emojis=PLAYER_EMOJIS[:2])
        store.capture('emoji', ['Vishwa'], [], complete=True, emojis=[PLAYER_EMOJIS[2]])
        with patch('backend.artwork.compose_artwork', return_value=b'png') as render:
            store.render('emoji')
            self.assertEqual(render.call_args.args[1], ['Vishwa', 'Amaya'])
            self.assertEqual(render.call_args.kwargs['emojis'], PLAYER_EMOJIS[:2])

    def test_empty_canvases_do_not_become_black_doodles(self):
        for mode, color in [('RGBA', (0, 0, 0, 0)), ('RGB', 'white'), ('RGB', 'black')]:
            with self.subTest(mode=mode, color=color):
                self.assertIsNone(drawing_ink(Image.new(mode, (100, 100), color)))
        source = Image.new('RGBA', (100, 100), (0, 0, 0, 0))
        output = io.BytesIO()
        source.save(output, format='PNG')
        thumb = thumbnail_bytes('data:image/png;base64,' + base64.b64encode(output.getvalue()).decode())
        with Image.open(io.BytesIO(thumb)) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
            self.assertIsNone(drawing_ink(image))

    def test_transparent_canvas_preserves_real_black_strokes(self):
        source = Image.new('RGBA', (350, 280), (0, 0, 0, 0))
        ImageDraw.Draw(source).line((20, 30, 80, 30), fill='black', width=5)
        output = io.BytesIO()
        source.save(output, format='PNG')
        thumb = thumbnail_bytes('data:image/png;base64,' + base64.b64encode(output.getvalue()).decode())
        with Image.open(io.BytesIO(thumb)) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
            self.assertEqual(image.getpixel((50, 30)), (0, 0, 0))
            self.assertEqual(drawing_ink(image).size, (61, 5))

    def test_single_pen_tap_is_not_enlarged_into_a_black_blob(self):
        for mode, background in [('RGB', 'white'), ('RGBA', (0, 0, 0, 0))]:
            source = Image.new(mode, (560, 560), background)
            ImageDraw.Draw(source).ellipse((270, 270, 285, 285), fill='black')
            output = io.BytesIO()
            source.save(output, format='PNG')
            thumb = thumbnail_bytes('data:image/png;base64,' + base64.b64encode(output.getvalue()).decode())
            with Image.open(io.BytesIO(thumb)) as image:
                self.assertIsNone(drawing_ink(image))
            # Extending the tap into a real stroke must keep that canvas.
            ImageDraw.Draw(source).line((277, 277, 370, 350), fill='black', width=15)
            self.assertIsNotNone(drawing_ink(source))

    def test_sour_gummy_title_font_is_bundled(self):
        from backend.artwork import TITLE_FONT_PATH
        from PIL import ImageFont
        face = ImageFont.truetype(str(TITLE_FONT_PATH), 52)
        self.assertEqual(face.getname()[0], 'Sour Gummy')
        face.set_variation_by_axes([100, 700])

    def test_all_rounds_both_players_and_no_winner_canvases_are_included(self):
        store = ArtworkStore()
        store.capture("token", ["Vishwa", "Amaya"], rounds(), complete=True)
        expected = []
        for item in rounds():
            for index, player in enumerate(item["drawing_players"]):
                canvas = drawing(item["round"] * 2 + index)
                store.attach("token", item["event_id"], player["id"], canvas)
                expected.append({"round": item["round"], "name": player["name"], "image": canvas})
        self.assertEqual(store.info("token")["received"], 12)
        with patch("backend.artwork.compose_artwork", wraps=compose_artwork) as render:
            png = store.render("token")
            self.assertEqual(len(render.call_args.args[2]), 12)
            self.assertEqual(render.call_args.args[2], expected)
            self.assertEqual(store.render("token"), png)
            self.assertEqual(render.call_count, 1)
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.size, (1600, 1600))

    def test_layout_is_random_per_game_but_repeatable_for_downloads(self):
        items = [{"image": drawing(i), "round": i + 1, "name": "Vishwa"} for i in range(6)]
        first = compose_artwork("one", ["Vishwa", "Amaya"], items)
        self.assertEqual(first, compose_artwork("one", ["Vishwa", "Amaya"], items))
        self.assertNotEqual(first, compose_artwork("two", ["Vishwa", "Amaya"], items))

    def test_waits_for_uploads_then_allows_missing_drawings_after_deadline(self):
        store = ArtworkStore()
        store.capture("token", ["Vishwa", "Amaya"], rounds(1), complete=True)
        with patch("backend.artwork.time.time", return_value=99):
            self.assertFalse(store.info("token")["ready"])
            with self.assertRaises(HTTPException) as error:
                store.render("token")
            self.assertEqual(error.exception.status_code, 409)
        with patch("backend.artwork.time.time", return_value=101):
            self.assertTrue(store.info("token")["ready"])
        before = store.render("token")
        store.attach("token", "event-1", "a", drawing())
        self.assertNotEqual(before, store.render("token"))

    def test_expiry_capacity_and_reset_remove_downloads(self):
        store = ArtworkStore(max_games=1, ttl=10)
        with patch("backend.artwork.time.time", return_value=100):
            store.capture("old", [], [], complete=True)
            store.capture("new", [], [], complete=True)
            with self.assertRaises(HTTPException):
                store.info("old")
        with patch("backend.artwork.time.time", return_value=111):
            with self.assertRaises(HTTPException):
                store.info("new")
        store.capture("reset", [], [], complete=True)
        store.clear()
        self.assertFalse(store.records)

    def test_unfinished_artwork_is_not_public(self):
        store = ArtworkStore()
        store.capture("token", [], [])
        with self.assertRaises(HTTPException):
            store.info("token")


class ArtworkGameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sharing = patch('backend.artwork.ARTWORK_SHARE_URL', '')
        sharing.start()
        self.addCleanup(sharing.stop)

    async def test_artwork_survives_rematch_and_preserves_original_names(self):
        with patch.object(GameManager, "initialize_leaderboard"):
            manager = GameManager(["cat", "dog"], {"test": ["cat", "dog"]})
        manager.broadcast = AsyncMock()
        game = Game(id="game", phase="FINAL_RESULT", round_winners=rounds(1))
        game.players = {"a": Player(id="a", name="Vishwa", seat=1), "b": Player(id="b", name="Amaya", seat=2)}
        manager.games[game.id] = game
        manager.game_locks[game.id] = asyncio.Lock()
        manager.capture_artwork(game, complete=True)
        old_token = game.artwork_id
        await manager.attach_round_drawing(game.id, "a", "event-1", drawing())
        await manager.attach_round_drawing(game.id, "b", "event-1", drawing(1))
        await manager.restart_game(game.id, "a")
        self.assertNotEqual(game.artwork_id, old_token)
        self.assertEqual(game.round_winners, [])
        self.assertEqual(manager.artworks.info(old_token)["names"], ["Vishwa", "Amaya"])
        self.assertEqual(manager.artworks.info(old_token)["received"], 2)

    async def test_download_headers_and_qr_url(self):
        with patch.object(GameManager, "initialize_leaderboard"):
            app = importlib.import_module("backend.app")
        store = ArtworkStore()
        store.capture("token", ["One", "Two"], [], complete=True)
        with patch.object(app.game_manager, "artworks", store), patch.object(app, "CORS_ORIGINS", ["https://example.com"]):
            response = app.artwork_image("token", download=True)
            self.assertIn("attachment", response.headers["content-disposition"])
            self.assertEqual(response.media_type, "image/png")
            with patch("qrcode.QRCode.add_data", autospec=True) as add:
                qr = app.artwork_qr("token", "https://example.com")
                self.assertEqual(add.call_args.args[1], "https://example.com/artwork/token")
                self.assertEqual(qr.media_type, "image/png")
            with self.assertRaises(HTTPException):
                app.artwork_qr("token", "https://unconfigured.example")


if __name__ == "__main__":
    unittest.main()
