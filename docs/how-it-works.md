# How it works

## Why the server does the policing

An ordinary webOS app gets suspended once it leaves the foreground, and its background services are time-limited. So an app on the TV can't watch the TV and block things all day. Instead, a daemon on a home server holds a WebSocket connection to the TV. Over it, the daemon gets pushes of power and foreground-app changes, sends toasts, closes apps, launches the quiz app and switches the TV off. The TV quiz app is only a display and a remote-control input. All state lives on the server.

## Components

| Component | File | Role |
|---|---|---|
| TV adapter | `server/src/ltp/tv/lg_webos.py` | SSAP over `wss://<tv>:3001` (bscpylgtv): state subscriptions, toast, close app, launch app, power off, remote key presses. Wake-on-LAN lives in `tv/wol.py`. A fake TV for tests lives in `tv/fake.py`. |
| Policy | `server/src/ltp/policy.py` | Pure functions that turn usage, allowance, curfew and lock state into a decision. Every rule change starts here, together with its tests. |
| App rules | `server/src/ltp/rules.py` | Per app: whether it counts toward the allowance, and when it may be used (always, only once time is up, or never during limits). |
| Monitor | `server/src/ltp/monitor.py` | Session accounting, enforcement, parent commands, daily summary. It decides while holding a lock and acts on the TV after releasing it, because TV actions come back as state pushes. |
| Store | `server/src/ltp/store.py` | SQLite holding sessions, credits (allowance ledger), events, key/value state and quiz attempts. The schema migrates forward on start-up. |
| Telegram | `server/src/ltp/notify.py` | Sends messages and long-polls for parent commands, answering only allowlisted chat ids. |
| Quiz | `server/src/ltp/quiz/` | Bank loader and validator (`bank.py`), the attempt state machine, grading and rewards (`service.py`, `passage.py`), and the LAN HTTP API (`http.py`). |
| Developer Mode keeper | `server/src/ltp/devmode.py` | Renews the TV's Developer Mode session every day and checks that the apps are still installed. |
| TV quiz app | `tv-app/quiz/` | A TypeScript single-page app for the TV's Chrome 79 engine. It handles remote-control focus and retries with backoff when the server is unreachable. |

## Time accounting and enforcement

- A session counts while the TV is on and an app that counts toward the allowance is in front. The home screen, launcher, settings, screensaver and quiz app don't count.
- Today's allowance is the free minutes, plus minutes earned from quizzes (both limited by the daily cap), plus parent grants (which aren't). Days roll over at local midnight in the configured time zone.
- Warnings appear as toasts a set number of minutes before time runs out (5 and 1 by default).
- When time is up, enforce mode closes the foreground app and, if the quiz is available, opens the quiz app. Reopening an app 3 times within 10 minutes switches the TV off. With `enforcement.curfew_power_off: true`, any open during curfew switches the TV off straight away. Parent messages for a run of back-to-back violations are merged: the first close and the first power-off are reported, the rest are counted and sent as one summary once 10 quiet minutes have passed.
- During curfew or a parent lock, nothing may be used, including the quiz.
- Observe mode runs every rule and records and reports what it would do, but never acts on the TV.
- If the TV drops off the network while a session is running, the parents are alerted.

## The quiz

The server owns every attempt. The TV app never sees an answer, and explanations arrive only once an attempt is finished.

- **One open attempt per tier.** Each device holds at most one open regular question and one open challenge question. Leaving the app, losing the network or reinstalling it brings back the same attempt with its timer and mistakes intact, so restarting never swaps the question. Unfinished attempts expire at local midnight.
- **Picking questions.** Regular questions are drawn from templates the child hasn't solved in the last 30 days, and the same kind is avoided twice in a row where possible. The challenge is one question per day, picked from a secret seed and the date, so nobody can work out future challenges in advance.
- **Answers.** Integers are compared by value. Decimals are compared exactly, so `1.5` equals `1.50`. Multiple choice uses fixed option ids, and the order is shuffled per attempt.
- **Mistakes.** A numeric question gets 3 tries, with a 60-second wait after each wrong one. Multiple choice forfeits the reward after one wrong answer. Ten wrong answers in a day pause the quiz for 15 minutes.
- **Hints.** Hints come in order. Using any hint halves the reward, rounded down.
- **Too fast.** A correct answer submitted sooner than the minimum thinking time (20 s regular, 180 s challenge) is recorded but earns nothing, and the parents are told.
- **Mistake book.** A regular question answered wrongly, with a hint, skipped or run out of tries enters the mistake book (`server/src/ltp/quiz/mistakes.py`, derived from attempt history). It comes back 1 day later, then 3, then 7; three clean answers in a row mark it mastered, and any new slip restarts the schedule. Every third regular question of the day is a due review. A review with different numbers earns the normal reward; a template with only one version is practice and earns nothing, because its answer was already shown. The daily summary lists open, due and mastered entries and today's new mistakes.
- **Skips.** The child can skip up to 3 regular questions a day. The challenge can't be skipped.
- **Rewards.** By default 10 minutes for a regular question and 25 for the challenge. `quiz.kind_minutes` sets a different reward for a family of regular kinds; vocabulary word sets (`vocab_*`) earn 3 minutes by default, and only when both steps are right. `quiz.kind_daily_limit` caps how many questions of a family are rewarded per day (3 word sets by default); later ones still count as practice. Rewards are capped by what is left under the daily cap. Notifications and stats always show the minutes actually credited.

### Reading sets with vocabulary

An English reading set is a single attempt: a passage, a few comprehension questions, then one or two vocabulary cards.

- Each question is answered once, in order.
- Each vocabulary card shows the word, its meaning and the sentence from the passage, followed by a check question. A wrong check sends the child back to re-read the card before trying again, with a short cooldown.
- The set is settled only after the last card. One wrong part halves the reward. A hint halves it again. Two or more wrong parts earn nothing.
- The minimum time for a whole set is 60 s regular and 300 s challenge.
- A skipped or expired set shows every answer and won't come back for 30 days.

### HTTP API

The API listens on the LAN only and checks every request three ways:

1. The source IP must be on the allowlist (only the TV by default), so a token copied to another device is useless.
2. There must be a bearer token for the device. It's compared in constant time and never logged.
3. There must be an API version header. An app/server mismatch tells the child to ask a parent to update the app.

Requests are also rate-limited per device, separately for reads and writes. Every write carries the attempt's revision number, and a stale write gets the current state back instead of being applied. Submissions are idempotent. Rejected requests are recorded as events with the source IP and the reason, never the token.

## Parent commands (Telegram)

| Command | Effect |
|---|---|
| `/status` | Today's usage, with buttons (+10, +30, lock/unlock, power, refresh) |
| `/add <minutes>` | Grant 1–180 minutes; grants ignore the daily cap |
| `/lock`, `/unlock` | Lock or unlock the TV immediately |
| `/on`, `/off` | Wake-on-LAN power-on (needs `LG_TV_MAC`; takes about a minute), or power-off |
| `/mode observe` or `/mode enforce` | Switch the mode |
| `/adult [minutes]`, `/kid` | Adult mode: nothing counts and nothing is enforced. It ends by itself when the TV turns off or after the given minutes (240 by default, at most 480), so the next power-on is always back in kid mode. `/kid` ends it at once |
| `/quiz` | Today's quiz details |
| `/quiz check` | A random live question with its answer, for spot-checking |
| `/quiz bad <id>` | Pull a question from the bank |

## Data

SQLite lives at `server/data/ltp.db` (configurable with `db_path`). It's ignored by git. To archive it, rename it with a timestamp instead of deleting it.
