# TEDx Pictionary — Scoring and QA Rules

**Code reviewed:** 9 November 2026. **Audience:** QA and event operators.

This is an internal reference for the current local implementation. Verify the
deployed build and saved Admin settings before using the numeric defaults below
as production expectations. Cosmetic drawing details are internal behavior and
should not be presented as player instructions. No application behavior was
changed to produce this document.

For user journeys, see [HIGH_LEVEL_USER_FLOWS.md](HIGH_LEVEL_USER_FLOWS.md).

## 1. Current defaults

| Setting | Current runtime default |
| --- | --- |
| Players per game | 2 |
| Maximum rounds | 6 |
| Round duration | 40 seconds |
| Countdown | 3 seconds |
| Model | 50-class TensorFlow model |
| Required stable guesses | 3 consecutive qualifying predictions |
| Pass confidence | 60%, represented as `0.6` |
| Gap between prediction attempts | 0.1 seconds after the response |
| Earliest normal winner acceptance | 2.5 seconds after drawing starts |
| Final round prediction delay | First 3.5 seconds |
| Late-round winner selection begins | 80% of the round duration |
| Round-result transition delay | At least 6 seconds after the decision |
| Disabled prompt classes | helicopter, bus, broccoli, giraffe, grapes |

Rounds 1–2 use easy classes, 3–4 medium, and 5–6 hard. Targets are randomly
selected from enabled classes in that difficulty and do not repeat in a game.
Difficulty affects target selection, not the scoring formula.

The normal configured game has six rounds. Internally, the maximum is limited
by the number of unique available classes, and a game can finish early if no
eligible unused target exists for the next difficulty.

## 2. Round points formula

Only the round winner receives points:

```text
remaining_seconds = max(0, ceil(round_deadline - winner_decision_time))
base_points       = 1000
time_bonus        = 10 × remaining_seconds
round_points      = base_points + time_bonus
```

`ceil` rounds a fractional second UP to the next integer. For example,
`ceil(19.01) = 20`; it is not ordinary rounding or rounding down.

The deadline is the server's drawing start time plus the configured duration.
The decision time is when the backend awards the round. The backend clock is
authoritative, not the timer displayed on a player's device. The countdown is
outside the drawing duration. Network and inference delays before the backend
decision can therefore affect the time bonus.

| Outcome | Winner points | Other player points |
| --- | --- | --- |
| Normal accepted recognition | `1000 + time_bonus` | 0 |
| Late-round confidence winner | Same formula | 0 |
| Winner selected at the deadline | Normally 1000 | 0 |
| No eligible winner | 0 | 0 |

There is no confidence multiplier, difficulty multiplier, final-round multiplier,
loser participation award or negative score. Confidence helps decide the winner;
it does not directly determine how many points that winner receives.

### Worked examples: a 40-second round

Assume a start time of zero and a deadline of 40. These are decision-time examples,
not timings measured from the browser's rounded timer.

| Decision time | Raw time remaining | Rounded remaining | Bonus | Winner points |
| --- | --- | --- | --- | --- |
| 2.5 s | 37.5 s | 38 | 380 | 1380 |
| 3.5 s | 36.5 s | 37 | 370 | 1370 |
| 10.0 s | 30.0 s | 30 | 300 | 1300 |
| 10.1 s | 29.9 s | 30 | 300 | 1300 |
| 20.0 s | 20.0 s | 20 | 200 | 1200 |
| 20.2 s | 19.8 s | 20 | 200 | 1200 |
| 32.0 s | 8.0 s | 8 | 80 | 1080 |
| 39.9 s | 0.1 s | 1 | 10 | 1010 |
| 40.0 s | 0.0 s | 0 | 0 | 1000 |

At rounding boundaries, compare exact server times: 20.0 seconds remaining awards
200 bonus points, while 20.0001 seconds remaining awards 210.

With the default acceptance delays, the earliest eligible ordinary-round decision
is 2.5 seconds and the earliest eligible final-round decision is 3.5 seconds.
For exactly six 40-second rounds, the corresponding ideal upper bound is:

```text
5 × 1380 + 1370 = 8270 points
```

This assumes a player wins every round at the earliest eligible instant. Actual
inference and delivery timing usually reduce that total. The upper bound changes
with Admin settings. A no-winner round always awards zero, even at timeout.

## 3. How a normal round winner is selected

Before 80% of the round has elapsed, the backend accepts a prediction only when:

1. The prediction's top class exactly matches the current target.
2. Its confidence is at least the configured threshold.
3. The player has the required number of consecutive qualifying predictions.
4. At least 2.5 seconds have elapsed since drawing started.

The required prediction count must also have been received. Every recorded wrong
class or below-threshold prediction resets the consecutive-hit count to zero.
Confidence exactly equal to the threshold qualifies. Only the top prediction
counts; having the target somewhere in the top-three list is insufficient.
Model confidence is rounded to four decimal places before it reaches this logic.

Default example for target `cat`, three hits and 60% confidence:

| Incoming top prediction | Confidence | Current streak | Outcome |
| --- | --- | --- | --- |
| cat | 0.70 | 1 | Continue drawing |
| cat | 0.60 | 2 | Continue drawing |
| dog | 0.90 | 0 | Wrong class resets streak |
| cat | 0.80 | 1 | Continue drawing |
| cat | 0.59 | 0 | Low confidence resets streak |
| cat | 0.65 | 1 | Continue drawing |
| cat | 0.75 | 2 | Continue drawing |
| cat | 0.90 | 3 | Wins if the minimum time has elapsed |

Qualifying hits can accumulate during the first 2.5 seconds of an ordinary round,
but a decision is not made until a qualifying prediction is processed at or after
2.5 seconds. In the final round, predictions are deferred for the first 3.5
seconds; deferred requests do not increment the streak or prediction count.

Normal wins use the first qualifying prediction processed by the backend. They
do not compare both players' confidence to choose the higher one. Nearly
simultaneous requests are serialized, so equal confidence is not automatically
a shared normal-round win.

## 4. Late-round and timeout winner selection

This is a separate rule from normal stable-guess acceptance.

```text
late_round_start = drawing_start + 0.8 × round_duration
```

For a 40-second round, late selection starts at **32 seconds**.

An eligible late-round candidate must have:

- At least one recorded prediction.
- A latest top prediction matching the target.
- A latest confidence strictly greater than zero.

Among eligible candidates, the player with the highest confidence wins. This
path does **not** require the configured pass threshold or stable-guess count.
The timer checks at 80%; subsequent predictions also run this selection. If no
winner is available, the timer checks again at the deadline.

| Latest stored predictions at selection time | Expected outcome |
| --- | --- |
| A: target at 0.15; B: wrong class at 0.95 | A wins |
| A: target at 0.40; B: target at 0.55 | B wins |
| A: target at 0.55; B: target at 0.55 | No winner yet; continue if time remains |
| Both top predictions are wrong | No winner yet |
| Target prediction with confidence 0 | Not eligible |
| No eligible candidate or an exact confidence tie at timeout | No round winner; both receive 0 |

Compare the latest stored prediction, not the player's best prediction from
earlier in the round. There is no prediction-age expiry in this selection, so a
previously recorded result can still qualify even if the player stopped drawing.
Rejected blank/invalid-image requests are not recorded as new predictions.

The round has one winner or no winner; points are never split between players.
There is no extra sudden-death period after an unresolved timeout.

## 5. Game totals, winner and final chart

```text
player_game_total = sum(points awarded for rounds that player won)
game_winner      = player with the highest game total
```

The number of round wins is not the final tie-breaker. Equal total points produce
a tied game, including a 0–0 game. A player can win fewer rounds but still win the
game through larger bonuses.

Example six-round game:

| Round | Winner | Points |
| --- | --- | --- |
| 1 | A | 1300 |
| 2 | B | 1250 |
| 3 | A | 1200 |
| 4 | No winner | 0 |
| 5 | B | 1100 |
| 6 | A | 1080 |

```text
A = 1300 + 1200 + 1080 = 3580
B = 1250 + 1100        = 2350
Game winner = A
```

On the final leaderboard chart, each colored segment is one won round's points.
The segments must add up to the displayed player total. The chart tooltip breaks
each segment into base points plus time bonus. The losing player's final screen
still displays their own points and global position.

## 6. Global leaderboard calculations

| Value | Calculation or rule |
| --- | --- |
| Displayed high score | Highest completed single-game total for that username |
| Position | Best score descending; equal scores ordered by username ascending |
| Public list | First 20 usernames |
| Player's final global position | Calculated across all ranked usernames, not just the first 20 |
| Personal-best flag | Current game score strictly exceeds the old best and equals the resulting best |
| Equal/lower replay score | Does not trigger a new personal best |
| First completed score of 0 | Can rank, but does not trigger the positive personal-best flag |

Example: Vishwa scores 3000 and 2500; Deelaka scores 1500 and 1075. The public
scores are Vishwa 3000 and Deelaka 1500, each appearing once. A later Deelaka score
of 4000 changes the order to Deelaka 4000, Vishwa 3000.

Equal scores get separate sequential positions according to name order, not a
shared rank. A personal best can occur without improving global position.

Scores are recorded at game completion, not after each round. Within one completed
game, repeated calls are guarded against adding that result twice. A completed
rematch counts as a new game. SQLite also retains lifetime totals, completed-game
counts and `wins`; `wins` counts **round wins**, not overall game victories. The
displayed score uses `best_score`, not the lifetime `score` column.

Username storage is case-sensitive, while active-session duplicate checks ignore
case. Test case variants explicitly; do not assume `Alice` and `alice` share a
historical leaderboard record. Leading/trailing whitespace is removed and internal
whitespace is collapsed. The name field accepts 1–24 characters before cleaning;
whitespace-only names are rejected.

## 7. Internal drawing and result timing

Scores are fixed at the backend decision. Players may continue drawing briefly
for the final images, without further inference or changes to winner/points.

| Round | Default extra drawing time |
| --- | --- |
| 1 | 0.5 s |
| 2 | 0.5 s |
| 3 | 1 s |
| 4 | 1.5 s |
| 5 | 3 s |
| 6 | 3 s |

The Admin page does not expose these controls. Backend configuration supports one
value per round, each from 0 to 10 seconds; zero disables the extra drawing time.

For winner rounds, the spectator result becomes visible after that window ends
and both participants' final drawings arrive, or after one additional second if
an image is missing. No-winner round results do not use that visibility wait.
The next-round/final-result transition waits at least
`max(6, extra_drawing_time + 1)` seconds after the decision. The extra drawing
window is not added to the round's score clock and does not reduce awarded points.

Prompt delivery is prepared 0.75 seconds ahead of the shared reveal time. Timing
checks should allow ordinary network jitter rather than require identical pixel
updates on separate devices at exactly the same instant.

## 8. Settings validation and persistence

| Admin setting | Allowed values |
| --- | --- |
| Round duration | Integer 10–600 seconds |
| Required stable guesses | Integer 1–10 |
| Prediction gap | 0.1–5 seconds |
| Confidence threshold | 1–100%, or 0.01–1 in the API |
| Class difficulty | easy, medium or hard |
| Enabled targets | At least two enabled classes in each difficulty |

The normal frontend sends all recognition settings on save. Important API detail:
if omitted from an Admin update body, the request schema currently supplies
**0.2 seconds, 2 hits and 30%**, which differs from the runtime startup defaults
of **0.1 seconds, 3 hits and 60%**. Record the actual settings in QA evidence.

Round duration, model, class pool and recognition rules are copied when a game
is created. Changes do not update that existing game, including its rematches.
The internal per-round drawing durations are refreshed at the start of a waiting
match/rematch. Admin settings are in memory and reset to code defaults on backend
restart. SQLite scores and emojis persist if the database file is retained.

Prediction pacing is a client wait after each response, so actual intervals also
include request/inference time. A configured 0.1-second gap does not guarantee
ten model predictions per second, and is not a server-enforced request rate limit.

## 9. Other QA behavior and boundaries

- **Blank/invalid canvas:** model endpoint returns 400; a blank canvas must not
  create a normal accepted prediction. Final artwork should omit blank ink.
- **Stale prediction:** a prediction finishing after a round/phase change is
  rejected for scoring. Old responses must not award points in a new round.
- **Deadline race:** a prediction request and timeout can arrive nearly together.
  Current code serializes them but does not independently reject predictions by
  elapsed deadline inside `record_prediction`. A late processed winner can receive
  the 1000-point base with zero bonus. Include this as a boundary/race test.
- **Admission:** only two seats can be occupied system-wide. Additional players
  join a FIFO queue, with automatic admission when a seat is released.
- **Leave/disconnect:** explicit leave removes the seat. Disconnected players
  have a 15-second reservation after detection; a silent WebSocket can first
  take up to 10 seconds to be detected. Active games pause, preserving scores,
  streaks and remaining time. Both players resume through a three-second
  countdown if reconnection succeeds; paused time/countdown does not reduce
  the time bonus. On expiry the match is cancelled, and the remaining player
  returns to waiting with the unfinished game's score reset. Empty games are
  removed. See [queue and reconnect details](QUEUE_AND_RECONNECT_QA.md).
- **Abandonment:** an unfinished game is not recorded as a completed score. A
  previously completed game's saved high score remains after leaving.
- **Rematch:** clears current points, round wins and used targets; both players
  ready up again. Previous completed-game records remain in SQLite.
- **Dashboard reset:** clears saved scores and old dashboard results, retains
  assigned emojis and does not end active games. An active game's later completion
  can legitimately add new scores after the reset.
- **Admin authentication:** password length 12–256; an unset/invalid configured
  password disables Admin access. Sessions last 8 hours; logout or a backend
  restart invalidates sessions. Five incorrect logins per client address within
  a minute cause temporary rate limiting on subsequent attempts.
- **Artwork:** both received drawings are eligible, including no-winner rounds.
  QR waits for successful hosted publication. Download failure must not change
  game points. In-memory source artwork can expire; the separately published PNG
  follows the image service's storage behavior.
- **Environment:** retain one worker/instance. Backend restart loses active games
  and Admin sessions. Test persisted high scores using the same SQLite file.
- **UI discrepancy to track:** the welcome screen currently advertises 30 seconds,
  while the runtime default is 40. Gameplay settings and server state are the
  scoring authority; report this copy mismatch separately.

## 10. Suggested QA evidence

For each scoring case, record the deployed version, Admin settings, round number,
target, both usernames, server deadline, winner decision time, prediction sequence,
confidence values, expected base/bonus/total and actual result. Capture the deadline
before the decision, because the backend clears the active deadline afterwards.

Use controlled backend tests for exact rounding and simultaneous-request cases.
Browser-only testing is useful for the user journey but cannot reliably force an
exact server decision time or a particular model confidence.

Source references: `backend/game_manager.py` (winner selection, points, rankings),
`backend/difficulty.py` (round difficulty), `backend/schemas.py` (API validation),
`backend/tensorflow_model_service.py` (prediction values), `backend/admin_auth.py`
(Admin sessions), and `frontend/src/components/ScoreBreakdown.jsx` (chart).
