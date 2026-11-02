import base64
import io

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError


def thumbnail_bytes(data_url: str) -> bytes:
    """Create a small, validated PNG in memory; never write drawings to disk."""
    try:
        if not data_url.startswith("data:image/png;base64,"):
            raise ValueError("Expected PNG")
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        with Image.open(io.BytesIO(raw)) as source:
            if source.format != "PNG" or max(source.size) > 2048:
                raise ValueError("Invalid dimensions or format")
            # Unpainted HTML canvases are transparent black. Flatten onto white
            # before dropping alpha, otherwise an empty drawing becomes black.
            rgba = source.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            background.alpha_composite(rgba)
            image = ImageOps.contain(background.convert("RGB"), (350, 280))
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise HTTPException(status_code=400, detail="Invalid round drawing.") from error
