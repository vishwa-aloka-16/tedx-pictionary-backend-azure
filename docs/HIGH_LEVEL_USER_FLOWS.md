# TEDx Pictionary — High-Level User Flows

This document describes how players, spectators and administrators use the system.
Use these flows for a high-level walkthrough and QA testing. The diagrams use
Mermaid and can be viewed in a Markdown viewer with Mermaid support.

## Before testing

- Open the game on two separate devices or browser profiles.
- Use two different player names.
- Open `/leaderboard` on a separate display to watch the game.
- Have the Admin password available for `/admin` testing.
- Use the configured round duration shown during gameplay.

## 1. Join and play a game

```mermaid
flowchart TD
    A[Open game website] --> B[Select Let's play]
    B --> C[Enter player name and select Join game]
    C --> D{Join successful?}
    D -->|No| E[Read message and correct the name or retry]
    E --> C
    D -->|Yes| Q{Player seat available?}
    Q -->|No| W[View queue position and wait for automatic admission]
    W --> F[Wait for the second player]
    Q -->|Yes| F
    F --> G[Both players select Ready]
    G --> H[Watch the countdown and read the target]
    H --> I[Both players draw the target]
    I --> J[View round result and points]
    J --> K{More rounds?}
    K -->|Yes| H
    K -->|No| L[View game winner, points and global position]
    L --> M{What next?}
    M -->|Play again| G
    M -->|Finish| N[Return to welcome page]
```

**Check:** Both players join the same game, see the same target and progress
through the rounds together. A recognized drawing or an expired round timer
leads to a result. The final screen identifies a win, loss or tie correctly.
The losing player's screen shows their own points as well as the winning points.

## 2. Follow a game on the leaderboard display

```mermaid
flowchart TD
    A[Open /leaderboard] --> B[View global high scores]
    B --> C[Optionally select Fullscreen]
    C --> D[Players start a game]
    D --> E[Watch player matchup and target announcement]
    E --> F[Follow game activity]
    F --> G[View both drawings and the round winner]
    G --> H{Game finished?}
    H -->|No| E
    H -->|Yes| I[View final points and round contribution chart]
    I --> J[View updated global leaderboard and artwork QR]
    J --> K[Final scoreboard stays until closed or a new game starts]
```

**Check:** The display reflects the active game and highlights each round winner.
Both players' submitted drawings are shown when available. Each username has one
global leaderboard entry showing its best single-game score, rather than the sum
of all games. Returning players keep their assigned emoji.

## 3. Get the game artwork

```mermaid
flowchart TD
    A[Finish a game] --> B[Wait for artwork preparation]
    B --> C[Artwork QR becomes available]
    C --> D[Scan QR using a phone]
    D --> E[Open the artwork link]
    E --> F[View the shared artwork]
    F --> G[Download or save the image]
```

**Check:** The QR opens a public address reachable from the phone. The artwork
uses both players' received drawings and includes branding, player names and
emojis. Blank canvases should not appear as filled black blocks. Artwork loading
should finish or show a useful error instead of leaving an endless spinner.

## 4. Manage the game as an administrator

```mermaid
flowchart TD
    A[Open /admin] --> B[Enter Admin password]
    B --> C{Password accepted?}
    C -->|No| D[Read the error and retry when allowed]
    D --> B
    C -->|Yes| E[View current game settings]
    E --> F{Choose action}
    F -->|Change settings| G[Adjust settings and save]
    G --> H[Start a new game to check the changes]
    F -->|Reset dashboard| I[Confirm reset and check cleared scores]
    H --> J[Sign out]
    I --> J
```

**Check:** Settings require a valid Admin login. Saved settings apply to newly
created games. Reset clears leaderboard scores while retaining player emojis;
active games can continue. Signing out returns to the login screen.

## 5. Handle connection loss or leaving

```mermaid
flowchart TD
    A[Player is waiting or playing] --> B{What happens?}
    B -->|Connection loss or screen lock| C[Game pauses and seat is reserved for 15 seconds]
    C --> D{Session still active?}
    D -->|Yes| R[Both players see a resume countdown]
    R --> E[Resume the same round with saved points and time]
    D -->|No| F[Unfinished match is cancelled and remaining player waits]
    B -->|Select Leave or Finish| G[Exit the current session]
    G --> H[Remaining player waits for an opponent]
    H --> I[If everyone leaves, a later join starts a fresh session]
```

**Check:** Refreshing or reconnecting must not create a second seat for the same
active player. A username already in use cannot occupy both seats. An unavailable
backend produces connection feedback and a retry path; successful connectivity
removes the connection notice.

## High-level QA completion checklist

- [ ] Two players can join, ready up and complete a game.
- [ ] Both players receive matching targets and round transitions.
- [ ] Recognition and timeout paths both finish rounds correctly.
- [ ] Winner, points and global positions are displayed correctly.
- [ ] Leaderboard activity shows both drawings when submitted.
- [ ] Reusing a name retains its emoji and only its best global score.
- [ ] QR works on a phone and the artwork can be saved.
- [ ] Play again and Finish follow the correct paths.
- [ ] Leaving, refreshing and reconnecting do not create duplicate players.
- [ ] Admin login, settings, dashboard reset and logout work as expected.
