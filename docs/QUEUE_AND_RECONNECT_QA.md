# Two-seat queue and reconnect behavior

## Admission and queue

- The backend admits at most two occupied player seats across the system.
- Additional players see **Game in progress—please wait** and a queue position.
- The queue is FIFO. A repeated join from the same browser reuses its existing
  player or queue ticket; an active/queued name cannot take another place.
- Seats reserved for disconnected players count toward the two-seat limit.
- When a seat is released through Finish/Exit or disconnect expiry, the first
  waiting player is assigned automatically. Two players still need to be ready
  and connected before a new game starts.
- Completed-game players retain their seats until they exit or their disconnect
  reservation expires. Completing a round/game does not silently remove a player.
- The queue screen polls every two seconds. It can be refreshed without losing
  the ticket stored in that tab. Leaving cancels the ticket, including a race
  where the server has just assigned a seat.
- Queue tickets inactive for 45 seconds are removed when queue state is checked.
  Keep the queue page open. This queue inactivity timeout is separate from the
  15-second reservation of an already occupied player seat.

## Disconnect, pause and resume

1. On a detected disconnect, an active round/countdown/result phase becomes
   `PAUSED` immediately. Its old timeout and in-flight prediction tokens become
   invalid. Both players' points, target, streaks and remaining time are retained.
2. The remaining player sees **Opponent disconnected. Waiting for reconnection…**
   and the reservation countdown. Drawing and recognition stop during the pause.
3. The disconnected seat is reserved for **15 seconds after server detection**.
4. On unlock/return to the page, the frontend immediately replaces the socket and
   fetches authoritative game state. It does not wait for the normal retry timer.
5. If both players reconnect in time, they see a **three-second resume countdown**.
   The same round resumes, without clearing the canvas on the retained page.
   The server shifts deadlines to exclude both the pause and resume countdown.
6. If the reservation expires, the unfinished match is cancelled and its partial
   score is not recorded as a completed game. The remaining player sees a clear
   waiting/cancellation message. If someone is queued, that player can immediately
   join the freed seat and appear in the waiting room.
7. If both players disappear, the empty game is removed. Previously completed
   SQLite high scores remain intact.

The browser sends `away` when the page becomes hidden, including screen lock
when the browser delivers that event. The server also detects a missing heartbeat:
clients send every three seconds, with a ten-second receive timeout. A device can
suspend abruptly without delivering a visibility event; in that case the server
must detect the missing heartbeat before starting the 15-second reservation.
The app cannot guarantee a live socket while a phone is locked.

Disconnecting during a resume countdown returns the match to pause. Disconnecting
during a result/image capture window freezes that window and the result transition
as well. Already-awarded points never change. A reconnect arriving at or after an
expired reservation is rejected even if its cleanup task has not run yet.

## API additions and state

- `POST /matchmaking/join` can now return
  `{ "queued": true, "queue_id": "...", "position": 1, "message": "..." }`.
  A seated response still contains `player_id` and `game`.
- `GET /matchmaking/queue/{queue_id}` returns the position, or
  `{ "queued": false, "player_id": "...", "game": { ... } }` on admission.
- `POST /matchmaking/queue/{queue_id}/leave` cancels a queue entry or a just-assigned
  seat. Queue IDs are opaque session capabilities; do not put real ones in reports.
- Player game snapshots include `connected` per player, `resume_phase`,
  `paused_remaining_seconds`, `reconnect_deadline` and `cancellation_message`.
- New game phases: `PAUSED` and `RESUMING`. Normal phases and scoring stay the same.
- A game WebSocket accepts text `ping` heartbeats and `away` visibility notices.

## Acceptance checks

| Test | Expected result |
| --- | --- |
| Three or more simultaneous joins | Exactly two seats; everyone else queues |
| Repeated join/refresh from queued browser | One queue ticket and unchanged order |
| One player leaves, with two people queued | First queued player admitted; second moves up |
| Cancel queue just as admission happens | Assigned seat released; no ghost player |
| Screen lock during drawing | Opponent sees pause; clock and score freeze |
| Unlock inside reservation | Immediate reconnect, three-second countdown, same target/ink/time |
| Old prediction finishes while paused | No points or round advancement |
| Old socket closes after replacement | New socket/session remains valid |
| Lock again during resume countdown | Paused again; no duplicate round transition |
| Reservation expires | Unfinished game cancelled; remaining player waits; FIFO admission continues |
| Both players leave/disconnect | Empty match removed; no occupied ghost seats |
| Pause during countdown | No extra target/round consumed |
| Pause during round result | Points fixed; result capture/transition windows preserved |
| Play again after completed game | Two connected players ready up; new game scores start at zero |

Deploy backend and frontend together. The backend remains a **single-worker,
single-instance** application with in-memory queue/match state; a process restart
clears these sessions. Azure's free-tier socket/resource limits still apply.
