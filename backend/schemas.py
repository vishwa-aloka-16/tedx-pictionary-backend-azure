from pydantic import BaseModel, Field, SecretStr
from typing import Annotated


class MatchmakingRequest(BaseModel):
    client_id: str | None = Field(default=None, min_length=16, max_length=100)
    name: str = Field(
        min_length=1,
        max_length=24,
    )


class AdminLoginRequest(BaseModel):
    password: SecretStr = Field(min_length=1, max_length=256)


class AdminSettingsRequest(BaseModel):
    cosmetic_draw_seconds_by_round: list[Annotated[float, Field(ge=0, le=10, allow_inf_nan=False)]] | None = Field(default=None, min_length=6, max_length=6)
    prediction_gap_seconds: float = Field(default=0.2, ge=0.1, le=5.0)
    required_hits: int = Field(default=2, ge=1, le=10, strict=True)
    confidence_threshold: float = Field(default=0.3, ge=0.01, le=1.0)
    class_difficulties: dict[str, str] | None = None
    model_key: str
    round_seconds: int = Field(
        ge=10,
        le=600,
    )
    excluded_classes: list[str] = Field(
        default_factory=list,
    )


class PlayerActionRequest(BaseModel):
    player_id: str


class GamePredictionRequest(
    PlayerActionRequest
):
    image_data_url: str = Field(
        min_length=1,
    )


class DrawingRequest(BaseModel):
    image_data_url: str = Field(
        min_length=1,
    )


class RoundDrawingRequest(PlayerActionRequest):
    event_id: str = Field(min_length=1, max_length=100)
    image_data_url: str = Field(min_length=1, max_length=500000)
