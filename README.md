# Learning-to-Play

Screen-time control for an LG webOS TV, run from a small home server. The TV counts as "play time", the child gets a daily allowance, and when it runs out they can earn more by answering maths, logic and English questions on the TV itself. Parents get Telegram notifications and can add time, lock the TV or switch it off from their phone.

Everything goes through the TV's official local control protocol (the same WebSocket API phone remote apps use). No rooting, no firmware changes.

> [!NOTE]
> Built for and tested on **one LG TV running webOS 6.0 (the 2021 lineup)**. Other pre-2022 webOS TVs will probably work but are untested. webOS 2022 and later have built-in usage limits, so you may not need this. See [docs/compatibility.md](docs/compatibility.md).

[中文说明](README.zh-CN.md)

## What it does

- **Tracks play time.** A daemon on your home server watches which app is in front on the TV and counts the minutes, skipping the home screen, settings and other exempt apps.
- **Enforces a daily allowance.** There is a free daily allowance, a daily cap, on-screen warnings before time runs out, a bedtime curfew and a parent lock. When time is up it closes the app; reopening it repeatedly switches the TV off.
- **Lets the child earn time.** A quiz app installed on the TV hands out questions, and correct answers add minutes up to the daily cap. The server decides everything (questions, grading, timing, rewards), so the TV app holds no answers and restarting it changes nothing.
- **Includes a question bank.** 142 question templates (263 questions) for ages 9–11: multi-step maths, logic puzzles and English reading sets. Each reading set ends with one or two vocabulary cards plus a check question. Charts and tables are drawn on the TV.
- **Keeps parents in control from Telegram.** Notifications when the TV turns on, when time is running out and when a quiz is solved, plus a daily summary. Commands: status, add minutes, lock or unlock, power on (Wake-on-LAN) or off, switch between observe and enforce mode, quiz details and spot checks.
- **Starts in observe mode.** By default it only records and notifies, and never touches the TV. Parents switch to enforce mode from Telegram once the numbers look right.

## How it fits together

```
 LG webOS TV                          Home server (always on, Linux)
 ┌──────────────────────┐   SSAP     ┌─────────────────────────────────────┐
 │ any app (YouTube…)   │◄──────────►│ ltp daemon                          │
 │                      │  WebSocket │  monitor ─ policy ─ store (SQLite)  │
 │ quiz app (.ipk)      │◄──────────►│  quiz service (HTTP, LAN only)      │
 └──────────────────────┘   HTTP     │  Telegram bot (parent commands)     │
                                     └─────────────────────────────────────┘
```

The TV can't be trusted to police itself: a webOS app gets suspended in the background. So the server watches the TV and acts on it. Details are in [docs/how-it-works.md](docs/how-it-works.md).

## What you need

- An LG webOS TV on your home network, with a reserved IP address.
- An always-on Linux machine on the same network (a Raspberry Pi, NUC or old laptop is enough) with Python 3.11+ and [uv](https://docs.astral.sh/uv/).
- A Telegram bot of your own (free, from @BotFather).
- For the quiz app only: a free LG developer account, Developer Mode enabled on the TV, and Node.js plus LG's `ares-cli` on the machine where you build the app.

## Getting started

Follow [docs/setup.md](docs/setup.md). The short version:

1. Clone the repo onto the server, then copy `server/.env.example` to `server/.env` and `server/config/settings.example.yaml` to `server/config/settings.yaml`.
2. Create a Telegram bot and fill in its token, your chat id and the TV's IP and MAC address.
3. Pair with the TV once (`uv run ltp pair --write-env`, then accept the prompt on the TV) and run `uv run ltp smoke`.
4. Install the systemd user service with `bash deploy/deploy.sh`.
5. Optional: enable the quiz service, then build and install the TV quiz app.

## Repository layout

| Path | What it is |
|---|---|
| `server/src/ltp/` | The daemon: TV adapter, policy rules, monitor, Telegram, storage, quiz service |
| `server/quiz/bank/` | Question bank (JSON) |
| `server/quiz/reviews/` | Review records for the question bank |
| `server/scripts/review_bank.py` | Sends questions to an LLM reviewer and writes review records |
| `server/config/settings.example.yaml` | All tunable rules, with comments |
| `tv-app/quiz/` | The TV quiz app (TypeScript, built for the TV's Chrome 79 engine) |
| `deploy/` | systemd user unit and deploy script |
| `docs/` | Setup, how it works, operations, question bank, compatibility |

## Development

- Server: in `server/`, run `uv sync`, then `uv run pytest`.
- TV app: in `tv-app/quiz/`, run `npm ci`, then `npm test` and `npm run build`. The build fails on JavaScript syntax newer than ES2019, so the app stays compatible with the TV's browser.

## Limitations

- **This is not tamper-proof.** A child who unplugs the network cable, switches the TV to a different Wi-Fi network or resets it can get around it. The server notices the TV going offline and alerts you, but it can't stop the TV. A smart plug or router-level block is the real hard stop; that isn't built yet.
- **Enforcement takes about a second.** The server closes an app after it opens rather than blocking the launch.
- **Developer Mode expires.** Apps installed through Developer Mode disappear when its session runs out. The daemon renews the session daily and alerts you if renewal fails.
- **Telegram messages are in Chinese.** The quiz app and the question bank are in English.
- **The question bank is AI-assisted.** The questions were written with help from an LLM and checked by an LLM reviewer (see [docs/question-bank.md](docs/question-bank.md)). Every maths answer is recomputed by the loader, and parents can spot-check questions and pull any of them from Telegram. Still, review them before relying on them.

## License

[MIT](LICENSE)
