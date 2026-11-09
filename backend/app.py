import asyncio
import time
import io
import qrcode
from contextlib import asynccontextmanager
from fastapi.responses import Response
from .round_drawing import thumbnail_bytes
from .config import CORS_ORIGINS
from .admin_auth import admin_auth, require_admin
from .leaderboard_gateway import LeaderboardUnavailable
from fastapi.responses import JSONResponse

from .tensorflow_model_service import get_model as warm_tensorflow_model

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)

from fastapi.middleware.cors import (
    CORSMiddleware,
)

from .game_manager import GameManager

from .tensorflow_model_service import (
    classes,
    metadata,
)
from .model_registry import (
    MODEL_CLASSES,
    get_available_models,
    predict_drawing,
)

from .schemas import (
    RoundDrawingRequest,
    AdminLoginRequest,
    AdminSettingsRequest,
    DrawingRequest,
    GamePredictionRequest,
    MatchmakingRequest,
    PlayerActionRequest,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Finish the 50-class model's cold start before accepting game requests.
    await asyncio.to_thread(warm_tensorflow_model)
    delivery = asyncio.create_task(game_manager.leaderboard_delivery_loop())
    try:
        yield
    finally:
        tasks = [delivery] + list(game_manager.disconnect_tasks.values()) + list(game_manager.connection_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title="AI Pictionary Game API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(LeaderboardUnavailable)
async def leaderboard_unavailable(_request, error):
    return JSONResponse(status_code=503, content={"detail": str(error)})


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=3600,
)


game_manager = GameManager(
    available_classes=classes,
    classes_by_model=MODEL_CLASSES,
)


@app.get("/")
def root():
    return {
        "name": "AI Pictionary Game API",
        "status": "running",
        "documentation": "/docs",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "server_time": time.time(),

        "model": {
            "model_key": game_manager.model_key,
            "number_of_classes": len(MODEL_CLASSES[game_manager.model_key]),
            "classes": MODEL_CLASSES[game_manager.model_key],

            "image_size": metadata.get(
                "image_size",
                28,
            ),

            "pixel_format": "black strokes on white background",
        },

        "game": {
            "maximum_players": 2,
            "maximum_rounds": 6,
            "round_seconds": game_manager.round_seconds,
        },
    }


@app.post("/admin/login")
def admin_login(request: AdminLoginRequest, client: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return admin_auth.login(request.password.get_secret_value(), client.client.host if client.client else "unknown")


@app.post("/admin/logout")
def admin_logout(token: str = Depends(require_admin)):
    admin_auth.logout(token)
    return {"status": "ok"}


@app.get("/admin", dependencies=[Depends(require_admin)])
def admin_settings():
    return {
        "models": get_available_models(game_manager.difficulties_by_model),
        "settings": game_manager.admin_state(),
    }


@app.put("/admin", dependencies=[Depends(require_admin)])
def update_admin_settings(
    request: AdminSettingsRequest,
):
    return {
        "models": get_available_models(game_manager.difficulties_by_model),
        "settings": game_manager.update_admin_settings(
            model_key=request.model_key,
            round_seconds=request.round_seconds,
            excluded_classes=request.excluded_classes,
            class_difficulties=request.class_difficulties,
            prediction_gap_seconds=request.prediction_gap_seconds,
            required_hits=request.required_hits,
            confidence_threshold=request.confidence_threshold,
            cosmetic_draw_seconds_by_round=request.cosmetic_draw_seconds_by_round,
        ),
    }


@app.post("/admin/reset-dashboard", dependencies=[Depends(require_admin)])
def reset_dashboard():
    return game_manager.reset_dashboard()


@app.get("/leaderbord")
@app.get("/leaderborad")
@app.get("/leaderboard")
def leaderboard():
    return game_manager.leaderboard()


# Standalone model test endpoint.
@app.post("/predict")
async def predict(
    request: DrawingRequest,
):
    return await asyncio.to_thread(
        predict_drawing,
        request.image_data_url,
        "tensorflow_50",
    )


# Player enters their name.
# The backend automatically creates or
# finds a waiting two-player game.
@app.post("/matchmaking/prepare")
async def prepare_matchmaking():
    return await game_manager.prepare_entry()


@app.post("/matchmaking/join")
async def join_matchmaking(
    request: MatchmakingRequest,
):
    game, player = (
        await game_manager.join_matchmaking(
            request.name,
            client_id=request.client_id,
        )
    )

    if game is None:
        return await game_manager.queue_status(player.id)

    return {
        "player_id": player.id,

        "game": game_manager.public_state(
            game
        ),
    }


@app.get("/matchmaking/queue/{queue_id}")
async def queue_status(queue_id: str):
    return await game_manager.queue_status(queue_id)


@app.post("/matchmaking/queue/{queue_id}/leave")
async def leave_queue(queue_id: str):
    return await game_manager.cancel_queue(queue_id)


@app.get("/games/{game_id}")
def get_game(
    game_id: str,
    player_id: str = Query(...),
):
    game = game_manager.get_game(
        game_id
    )

    game_manager.get_player(
        game,
        player_id,
    )

    return game_manager.public_state(
        game
    )


@app.post("/games/{game_id}/ready")
async def player_ready(
    game_id: str,
    request: PlayerActionRequest,
):
    return await game_manager.mark_ready(
        game_id=game_id,
        player_id=request.player_id,
    )


@app.post("/games/{game_id}/predict")
async def game_prediction(
    game_id: str,
    request: GamePredictionRequest,
):
    game = game_manager.get_game(
        game_id
    )

    game_manager.get_player(
        game,
        request.player_id,
    )

    if game.phase != "DRAWING":
        raise HTTPException(
            status_code=409,
            detail=(
                "The round is not accepting "
                "drawings."
            ),
        )

    # Skip inference on early snapshots; the client's next request contains
    # the drawing after the player has had time to develop it.
    waiting = game_manager.prediction_wait_response(game)
    if waiting is not None:
        return waiting

    round_token = game.transition_token
    prediction_result = (
        await asyncio.to_thread(
            predict_drawing,
            request.image_data_url,
            game.model_key,
        )
    )

    return await game_manager.record_prediction(
        game_id=game_id,
        player_id=request.player_id,
        prediction_result=prediction_result,
        round_token=round_token,
    )


@app.post("/games/{game_id}/restart")
async def restart_game(
    game_id: str,
    request: PlayerActionRequest,
):
    return await game_manager.restart_game(
        game_id=game_id,
        player_id=request.player_id,
    )


@app.post("/games/{game_id}/round-drawing")
async def round_drawing(game_id: str, request: RoundDrawingRequest):
    game = game_manager.get_game(game_id)
    game_manager.get_player(game, request.player_id)
    drawing = await asyncio.to_thread(thumbnail_bytes, request.image_data_url)
    return await game_manager.attach_round_drawing(game_id, request.player_id, request.event_id, drawing)


@app.get("/round-drawings/{event_id}")
def get_round_drawing(event_id: str):
    drawing = game_manager.round_drawings.get(event_id)
    if drawing is None:
        raise HTTPException(status_code=404, detail="Drawing is no longer available.")
    return Response(drawing, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/artworks/{artwork_id}")
def artwork_info(artwork_id: str):
    return game_manager.artworks.info(artwork_id)


@app.get("/artworks/{artwork_id}/image")
def artwork_image(artwork_id: str, download: bool = False):
    png = game_manager.artworks.render(artwork_id)
    headers = {"Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="jkit-drawing-story.png"'
    return Response(png, media_type="image/png", headers=headers)


@app.get("/artworks/{artwork_id}/qr")
def artwork_qr(artwork_id: str, frontend_origin: str | None = Query(default=None, max_length=300)):
    info = game_manager.artworks.info(artwork_id)
    if info["sharing_enabled"]:
        if not info["share_url"]:
            raise HTTPException(409, "Artwork is being uploaded. Try again shortly.")
        url = info["share_url"]
    else:
        if frontend_origin not in CORS_ORIGINS:
            raise HTTPException(400, "Frontend origin is not configured for artwork sharing.")
        url = f"{frontend_origin}/artwork/{artwork_id}"
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=7, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    output = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(output, format="PNG")
    return Response(output.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/games/{game_id}/leave")
async def leave_game(
    game_id: str,
    request: PlayerActionRequest,
):
    await game_manager.leave_game(
        game_id=game_id,
        player_id=request.player_id,
    )

    return {
        "status": "left",
    }


@app.websocket("/ws/leaderboard")
async def leaderboard_websocket(websocket: WebSocket):
    await websocket.accept()
    updates = asyncio.Queue(maxsize=1)
    game_manager.leaderboard_subscribers.add(updates)
    try:
        while True:
            try:
                rows = await asyncio.to_thread(game_manager.leaderboard_rows)
            except LeaderboardUnavailable:
                await websocket.close(code=1013, reason="Leaderboard storage is starting. Please reconnect.")
                return
            snapshot = game_manager.leaderboard(rows)
            await websocket.send_json(snapshot)
            try:
                # Winner decisions wake this immediately. Periodic snapshots
                # also refresh joins, resets, and completed-game totals.
                await asyncio.wait_for(updates.get(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        game_manager.leaderboard_subscribers.discard(updates)


@app.websocket("/ws/games/{game_id}")
async def game_websocket(
    websocket: WebSocket,
    game_id: str,
    player_id: str,
):
    try:
        game = game_manager.get_game(
            game_id
        )

        game_manager.get_player(
            game,
            player_id,
        )

    except HTTPException:
        await websocket.close(
            code=4403
        )

        return

    await websocket.accept()

    try:
        connected = await game_manager.connect_websocket(
            game=game,
            player_id=player_id,
            websocket=websocket,
        )
    except HTTPException:
        await websocket.close(code=4403)
        return
    if not connected:
        return

    try:
        while True:
            # Receives the frontend's
            # WebSocket heartbeat.
            message = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            if message == "away":
                await websocket.close(code=1001)
                break

    except WebSocketDisconnect:
        pass

    except asyncio.TimeoutError:
        await websocket.close(code=1001)

    except Exception:
        pass

    finally:
        game_manager.disconnect_websocket(
            game_id=game_id,
            websocket=websocket,
        )
