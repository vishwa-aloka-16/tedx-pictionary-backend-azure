from fastapi import HTTPException
from .difficulty import default_difficulties

from . import tensorflow_model_service as tensorflow_50


MODEL_SERVICES = {
    "tensorflow_50": {
        "name": "50 Classes",
        "description": (
            "More drawing variety"
        ),
        "classes": tensorflow_50.classes,
        "predict": (
            tensorflow_50.predict_drawing
        ),
    },
}


MODEL_CLASSES = {
    model_key: service["classes"]
    for model_key, service
    in MODEL_SERVICES.items()
}


def get_available_models(difficulties_by_model=None) -> list[dict]:
    return [
        {
            "key": model_key,
            "name": service["name"],
            "description": (
                service["description"]
            ),
            "number_of_classes": len(
                service["classes"]
            ),
            "classes": service["classes"],
            "class_difficulties": (difficulties_by_model or {}).get(
                model_key, default_difficulties(service["classes"])),
        }
        for model_key, service
        in MODEL_SERVICES.items()
    ]


def predict_drawing(
    image_data_url: str,
    model_key: str,
) -> dict:
    service = MODEL_SERVICES.get(
        model_key
    )

    if service is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown model: {model_key}"
            ),
        )

    result = service["predict"](
        image_data_url
    )

    return {
        **result,
        "model_key": model_key,
    }
