"""Temporary game souvenirs, composed from actual player drawings in memory."""
import io
import math
import logging
import random
import threading
import time
from collections import OrderedDict
from pathlib import Path

from fastapi import HTTPException
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps
from .config import ARTWORK_SHARE_URL, ARTWORK_SHARE_UPLOAD_KEY
from .artwork_publisher import ArtworkPublisher

ASSETS_PATH = Path(__file__).resolve().parent / "assets/logos"
JKIT_LOGO_PATH = ASSETS_PATH / "JKIT-logo-short.jpg"
TEDX_LOGO_PATH = ASSETS_PATH / "logo-black-colombo.png"
CENTER_SCALE = 0.6
CENTER_RADIUS = 280 * CENTER_SCALE
EMOJI_FONT_PATH = Path(__file__).resolve().parent / "assets/fonts/NotoEmoji.ttf"
TITLE_FONT_PATH = Path(__file__).resolve().parent / "assets/fonts/SourGummy.ttf"
ARTWORK_TTL = 3600


def font(size, bold=False, italic=False):
    if bold and italic:
        paths = ("DejaVuSans-BoldOblique.ttf", "arialbi.ttf")
    else:
        paths = ("DejaVuSans-Bold.ttf", "arialbd.ttf") if bold else ("DejaVuSans.ttf", "Arial.ttf")
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def drawing_ink(source):
    """Extract visible strokes, never the transparent canvas background."""
    rgba = source.convert("RGBA")
    background = Image.new("RGBA", rgba.size, "white")
    background.alpha_composite(rgba)
    gray = background.convert("L")
    # Also omit uniform black thumbnails made by the old alpha-dropping path.
    low, high = gray.getextrema()
    if low == high:
        return None
    ink = ImageOps.invert(gray)
    bounds = ink.point(lambda p: 255 if p > 25 else 0).getbbox()
    if not bounds:
        return None
    # A tap leaves a small round pen dot. Enlarging its tight crop to a full
    # doodle makes an almost-empty canvas appear as repeated black blobs.
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    minimum_extent = max(8, max(source.size) * 0.04)
    if max(width, height) < minimum_extent:
        return None
    return ink.crop(bounds)


def artwork_signature(names, emojis):
    """Pair each name with its saved avatar using a monochrome emoji font."""
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    for size in range(38, 11, -1):
        face = font(size)
        emoji_face = ImageFont.truetype(str(EMOJI_FONT_PATH), size + 10) if any(emojis) else None
        parts = []
        for index, name in enumerate(names[:2]):
            if index:
                parts.append((" x ", face))
            if index < len(emojis) and emojis[index]:
                parts.append((emojis[index], emoji_face))
                parts.append((" ", face))
            parts.append((name, face))
        width = sum(probe.textlength(text, font=part_face) for text, part_face in parts)
        if width <= 1400:
            break
    signature = Image.new("RGBA", (max(1, int(width) + 4), 64))
    painter = ImageDraw.Draw(signature)
    x = 2
    for text, part_face in parts:
        painter.text((x, 32), text, font=part_face, fill="black", anchor="lm")
        x += probe.textlength(text, font=part_face)
    return signature


def artwork_logo(path, size, monochrome=False):
    """Trim transparent or white padding without changing the brand colors."""
    with Image.open(path) as source:
        logo = source.convert("RGBA")
    background = Image.new("RGBA", logo.size, "white")
    background.alpha_composite(logo)
    if monochrome:
        # Remove the short logo JPEG's pale gray backdrop, including its cutouts.
        ink = background.convert("L").point(lambda p: 255 - p if p < 185 else 0)
        logo = Image.new("RGBA", logo.size, "black")
        logo.putalpha(ink)
        bounds = ink.getbbox()
        return ImageOps.contain(logo.crop(bounds) if bounds else logo, size,
                               Image.Resampling.LANCZOS)
    bounds = ImageChops.difference(background.convert("RGB"),
                                  Image.new("RGB", logo.size, "white")).getbbox()
    return ImageOps.contain(logo.crop(bounds) if bounds else logo, size,
                           Image.Resampling.LANCZOS)


def compose_artwork(token, names, drawings, emojis=None):
    """Repeat real drawings with occasional red ink and a white brand area."""
    rng = random.Random(token)
    image = Image.new("RGBA", (1600, 1600), "white")
    painter = ImageDraw.Draw(image)
    signature = artwork_signature(names, emojis or [])
    # A gently scalloped white area leaves room for the central branding.
    def badge_points(padding=0, scale=1, offset=(0, 0)):
        points = []
        for step in range(361):
            angle = math.tau * step / 360
            radius = (CENTER_RADIUS + padding + CENTER_SCALE * (
                14 * math.sin(5 * angle + .4) + 9 * math.sin(9 * angle - .7)
                + 4 * math.sin(13 * angle)))
            points.append(((800 + radius * math.cos(angle) - offset[0]) * scale,
                           (800 + radius * math.sin(angle) - offset[1]) * scale))
        return points

    clearance = Image.new("L", image.size)
    ImageDraw.Draw(clearance).polygon(badge_points(padding=12 * CENTER_SCALE), fill=255)
    # Reserve just the signature and the organic badge, leaving the rest for art.
    occupied = [(1548 - signature.width - 18, 1496, 1576, 1584)]
    placed_count = 0
    masks = []
    for item in drawings:
        with Image.open(io.BytesIO(item["image"])) as source:
            ink = drawing_ink(source)
            if ink is not None:
                masks.append(ink)

    def place(mask, target):
        nonlocal placed_count
        # Thicken at twice the output size for bold, smooth small doodles.
        ink = ImageOps.contain(mask, (target * 2, target * 2), Image.Resampling.LANCZOS)
        ink = ink.filter(ImageFilter.MaxFilter(3))
        ink = ink.resize((max(1, ink.width // 2), max(1, ink.height // 2)),
                         Image.Resampling.LANCZOS)
        ink = ink.rotate(rng.uniform(-150, 150), Image.Resampling.BICUBIC, expand=True)
        bounds = ink.getbbox()
        if not bounds:
            return False
        ink = ink.crop(bounds)
        width, height = ink.size
        for _ in range(350):
            x = rng.randint(32, 1568 - width)
            y = rng.randint(32, 1568 - height)
            if clearance.crop((x, y, x + width, y + height)).getbbox():
                continue
            if any(x < right + 4 and x + width + 4 > left
                   and y < bottom + 4 and y + height + 4 > top
                   for left, top, right, bottom in occupied):
                continue
            # Sparse red accents use the players' actual sketches too.
            color = "#e62323" if placed_count % 13 == 5 else "#000000"
            tile = Image.new("RGBA", ink.size, color)
            tile.putalpha(ink)
            image.alpha_composite(tile, (x, y))
            occupied.append((x, y, x + width, y + height))
            placed_count += 1
            return True
        return False

    # Place every received nonblank drawing before filling gaps with repetitions.
    rng.shuffle(masks)
    for mask in masks:
        for target in (rng.randint(55, 75), 45, 35, 25):
            if place(mask, target):
                break
    if masks:
        # Cycle through every canvas equally, so neither player's drawings
        # dominate the repetitions just because of random selection.
        for target_range, count in (((45, 70), 300), ((30, 50), 400), ((20, 35), 400)):
            remaining = []
            for _ in range(count):
                if not remaining:
                    remaining = list(masks)
                    rng.shuffle(remaining)
                place(remaining.pop(), rng.randint(*target_range))

    # Supersample only the white center, keeping its curved edge smooth.
    badge = Image.new("RGBA", (640 * 3, 640 * 3))
    badge_pen = ImageDraw.Draw(badge)
    points = badge_points(scale=3, offset=(480, 480))
    badge_pen.polygon(points, fill="white")
    image.alpha_composite(badge.resize((640, 640), Image.Resampling.LANCZOS), (480, 480))
    painter = ImageDraw.Draw(image)
    jkit = artwork_logo(JKIT_LOGO_PATH, (108, 108))
    tedx = artwork_logo(TEDX_LOGO_PATH, (260, 60))
    image.alpha_composite(jkit, (800 - jkit.width // 2, 734 - jkit.height // 2))
    image.alpha_composite(tedx, (800 - tedx.width // 2, 830 - tedx.height // 2))
    title_face = ImageFont.truetype(str(TITLE_FONT_PATH), 31)
    title_face.set_variation_by_axes([100, 700])
    painter.text((800, 890), "2026", font=title_face, fill="#e62323", anchor="mm")
    image.alpha_composite(signature, (1548 - signature.width, 1508))
    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG")
    return output.getvalue()


class ArtworkStore:
    def __init__(self, max_games=16, ttl=ARTWORK_TTL, publisher=None):
        self.records = OrderedDict()
        self.max_games = max_games
        self.ttl = ttl
        self.lock = threading.RLock()
        # Never generate multiple large PNGs simultaneously on a small instance.
        self.render_lock = threading.Lock()
        self.publisher = publisher or (ArtworkPublisher(ARTWORK_SHARE_URL, ARTWORK_SHARE_UPLOAD_KEY)
                                       if ARTWORK_SHARE_URL else None)
        self.warming_up = False

    def warm_up(self):
        with self.lock:
            if not self.publisher or self.warming_up:
                return
            self.warming_up = True
            threading.Thread(target=self._warm_up, daemon=True).start()

    def _warm_up(self):
        try:
            self.publisher.warm_up()
        except Exception as error:
            # A sleeping/offline image service must never prevent a game start.
            logging.getLogger(__name__).info("Artwork service warm-up failed (%s).", type(error).__name__)
        finally:
            with self.lock:
                self.warming_up = False

    def prune(self):
        for token, record in list(self.records.items()):
            if record["expires_at"] <= time.time():
                self.records.pop(token, None)
        while len(self.records) > self.max_games:
            self.records.popitem(last=False)

    def capture(self, token, players, rounds, complete=False, emojis=None):
        with self.lock:
            self.prune()
            record = self.records.setdefault(token, {"names": list(players), "emojis": list(emojis or []), "drawings": {}, "complete": False,
                                                     "png": None, "expires_at": time.time() + self.ttl})
            record["rounds"] = [{"event_id": r["event_id"], "round": r["round"],
                                "players": r.get("drawing_players", []),
                                "winner_id": r.get("winner_id")} for r in rounds]
            record["expected"] = sum(len(r["players"]) for r in record["rounds"])
            record["ready_at"] = max((r.get("drawing_deadline", 0) for r in rounds), default=0)
            record["complete"] |= complete
            record["expires_at"] = time.time() + self.ttl
            self.prune()
            self._start_publication(token, record)

    def attach(self, token, event_id, player_id, image):
        with self.lock:
            record = self.records.get(token)
            if record and (event_id, player_id) not in record["drawings"]:
                record["drawings"][(event_id, player_id)] = image
                record["png"] = None
                record["share_url"] = None

    def _start_publication(self, token, record):
        """Called under the store lock; never block gameplay on rendering/upload."""
        ready = len(record["drawings"]) >= record.get("expected", 0) or time.time() >= record.get("ready_at", 0)
        if (not self.publisher or not record["complete"] or not ready
                or record.get("publishing") or record.get("share_url")
                or time.time() < record.get("publish_retry_at", 0)):
            return
        record["publishing"] = True
        threading.Thread(target=self._publish, args=(token, record), daemon=True).start()

    def _publish(self, token, record):
        try:
            with self.lock:
                version = len(record["drawings"])
            url = self.publisher(token, self.render(token))
            with self.lock:
                if self.records.get(token) is record and version == len(record["drawings"]):
                    record["share_url"] = url
                    record["publish_retry_at"] = 0
        except Exception as error:
            logging.getLogger(__name__).warning("Artwork publication failed (%s); it can be retried.", type(error).__name__)
            with self.lock:
                record["publish_retry_at"] = time.time() + 5
        finally:
            with self.lock:
                record["publishing"] = False

    def _get(self, token):
        self.prune()
        record = self.records.get(token)
        if not record or not record["complete"]:
            raise HTTPException(404, "This artwork is unavailable or has expired.")
        return record

    def info(self, token):
        with self.lock:
            record = self._get(token)
            self._start_publication(token, record)
            return {"id": token, "names": record["names"], "received": len(record["drawings"]),
                    "expected": record["expected"], "expires_at": record["expires_at"],
                    "sharing_enabled": bool(self.publisher), "share_url": record.get("share_url"),
                    "share_status": "ready" if record.get("share_url") else "retrying" if record.get("publish_retry_at") else "uploading",
                    "ready": len(record["drawings"]) >= record["expected"] or time.time() >= record["ready_at"]}

    def render(self, token):
        with self.render_lock:
            with self.lock:
                record = self._get(token)
                if not self.info(token)["ready"]:
                    raise HTTPException(409, "Finishing the last drawings. Try again in a moment.")
                if record["png"]:
                    return record["png"]
                drawings = [{"round": r["round"], "name": player["name"],
                             "image": record["drawings"][(r["event_id"], player["id"])]}
                            for r in record["rounds"] for player in r["players"]
                            if (r["event_id"], player["id"]) in record["drawings"]]
                names = list(record["names"])
                emojis = list(record["emojis"])
                version = len(record["drawings"])
            png = compose_artwork(token, names, drawings, emojis=emojis)
            with self.lock:
                if self.records.get(token) is record and version == len(record["drawings"]):
                    record["png"] = png
            return png

    def clear(self):
        with self.lock:
            self.records.clear()
