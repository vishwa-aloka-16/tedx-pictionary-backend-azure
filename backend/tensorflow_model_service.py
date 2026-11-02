import base64
import io
import json
import threading
import time
from pathlib import Path

import numpy as np
from fastapi import HTTPException
from PIL import Image, ImageChops, ImageOps, UnidentifiedImageError


MODEL_DIR = Path(__file__).parent / "models"
METADATA_PATH = MODEL_DIR / "metadata_keras.json"
MODEL_PATH = MODEL_DIR / "best_model.keras"

with METADATA_PATH.open(encoding="utf-8") as metadata_file:
    metadata = json.load(metadata_file)

classes = metadata["classes"]["class_names"]
model = None
model_lock = threading.Lock()
inference_lock = threading.Lock()


def get_model():
    global model

    if model is None:
        with model_lock:
            if model is None:
                try:
                    from tensorflow import keras
                except ImportError as error:
                    raise RuntimeError(
                        "TensorFlow is not installed. Run: pip install -r requirements.txt"
                    ) from error
                loaded_model = keras.models.load_model(
                    MODEL_PATH,
                    compile=False,
                )
                # Build the prediction function and initialize TensorFlow kernels
                # before exposing the model to gameplay requests.
                loaded_model.predict(
                    np.ones((1, 28, 28, 1), dtype=np.float32), verbose=0,
                )
                model = loaded_model

    return model


def prepare_canvas(image_data_url: str) -> np.ndarray:
    try:
        encoded_image = image_data_url.split(",", 1)[1]
        image_bytes = base64.b64decode(encoded_image, validate=True)
        image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    except (IndexError, ValueError, UnidentifiedImageError, OSError) as error:
        raise HTTPException(status_code=400, detail="Invalid canvas image.") from error

    background = Image.new("RGBA", image.size, "white")
    grayscale = Image.alpha_composite(background, image).convert("L")
    ink = ImageChops.invert(grayscale)
    bounds = ink.point(lambda pixel: 255 if pixel > 18 else 0).getbbox()

    if bounds is None:
        raise HTTPException(status_code=400, detail="Draw something first.")

    cropped = grayscale.crop(bounds)
    fitted = ImageOps.contain(cropped, (20, 20), Image.Resampling.LANCZOS)
    normalized = Image.new("L", (28, 28), 255)
    normalized.paste(
        fitted,
        ((28 - fitted.width) // 2, (28 - fitted.height) // 2),
    )

    return (np.asarray(normalized, dtype=np.float32) / 255.0).reshape(
        1, 28, 28, 1
    )


def predict_drawing(image_data_url: str) -> dict:
    start = time.perf_counter()
    image = prepare_canvas(image_data_url)

    with inference_lock:
        probabilities = np.asarray(
            get_model().predict(image, verbose=0)[0],
            dtype=np.float32,
        )

    top_indices = np.argsort(probabilities)[-3:][::-1]
    top_predictions = [
        {
            "prediction": classes[int(index)],
            "confidence": round(float(probabilities[index]), 4),
        }
        for index in top_indices
    ]
    best_index = int(top_indices[0])

    return {
        "prediction": classes[best_index],
        "confidence": round(float(probabilities[best_index]), 4),
        "inference_ms": round((time.perf_counter() - start) * 1000, 1),
        "top_predictions": top_predictions,
    }
