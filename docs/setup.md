# Setup

This guide takes you from a fresh clone to a running daemon, and optionally to the quiz app on the TV. Every path is relative to the repository root. Commands under "server" run in `server/`.

## 1. Prepare the network

- In your router, reserve a fixed IP address for the TV. You'll find the TV's current IP and MAC address under Settings → Network → connection details. Use the MAC of the interface the TV actually uses (Wi-Fi or wired); it's needed for remote power-on.
- Pick an always-on Linux machine on the same network as the server. It needs Python 3.11+ and uv (`curl -LsSf https://astral.sh/uv/install.sh | sh` installs it to `~/.local/bin/uv`).

## 2. Create a Telegram bot

1. In Telegram, message @BotFather, send `/newbot` and keep the token it gives you. Use a new bot for this project only.
2. From each parent's own Telegram account, send the new bot any message.
3. Open `https://api.telegram.org/bot<token>/getUpdates` in a browser and find `"chat":{"id":…}`. In a private chat this is the parent's user id. If both parents want access, collect both ids.

The bot only answers chat ids listed in `.env`. It ignores messages from anyone else and logs them.

## 3. Install and configure the server

1. Clone the repository onto the server and run `uv sync` in `server/`.
2. Copy `server/.env.example` to `server/.env` and fill in `LG_TV_IP`, `LG_TV_MAC`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (separate several ids with commas). `.env` holds secrets: keep it out of git (it's already ignored) and make it readable only by you (`chmod 600`).
3. Copy `server/config/settings.example.yaml` to `server/config/settings.yaml` and set at least `timezone` (an IANA name such as `Europe/London`). Every rule is explained by a comment in the file: free minutes, daily cap, warnings, curfew, how many reopenings trigger power-off, exempt apps and summary time. `settings.yaml` is also ignored by git, so your values never end up in commits.

## 4. Pair with the TV

Do this once, with the TV on and someone holding its remote.

1. On the server, run `uv run ltp pair --write-env`. The TV asks whether to allow the connection. Choose Allow, and the client key is written into `.env`.
2. Run `uv run ltp smoke`. A "connected" toast should appear on the TV. Switch between a few apps and watch the terminal print each change. Add `--power-off` to test remote power-off as well.

The daemon never pairs by itself, because a pairing prompt would pop up while the child is watching. If the key is lost, pair again by hand.

## 5. Run it as a service

1. Run `bash deploy/deploy.sh` in the repository root. It refuses to run if the working tree has uncommitted changes. It fast-forwards to the latest commit, runs `uv sync`, renders the systemd user unit from `deploy/ltp.service` with your paths filled in, then enables and restarts it. It also copies the two config templates if they are missing.
2. User services stop when you log out unless lingering is enabled. Run `sudo loginctl enable-linger $USER` once; the script warns you if it's off.
3. When Telegram shows the "monitor started (observe mode)" message, the service is running.
4. Leave it in observe mode for a few days and compare the reported usage with what you actually saw. When it matches, send `/mode enforce` to the bot.

## 6. Optional: the quiz app

The quiz service runs inside the daemon and only answers the TV. The TV app is installed through Developer Mode.

### 6.1 Enable the quiz service

1. Generate a device token with `python -c "import secrets;print(secrets.token_urlsafe(24))"`.
2. Put it in `server/.env` as `QUIZ_TOKENS=tv:<token>`. The format is `device:token`; separate several devices with commas.
3. In `server/config/settings.yaml`, set `quiz.enabled: true`. The other quiz settings are optional. By default the service listens on port 8765 and accepts requests only from `LG_TV_IP`.
4. If the server runs a firewall, allow only the TV through. With ufw: `sudo ufw allow from <tv-ip> to any port 8765 proto tcp`.
5. Restart the service. If the bank fails validation, the port is taken or no token is set, the quiz turns itself off and Telegram gets one alert. Monitoring keeps running either way.

### 6.2 Enable Developer Mode on the TV

1. Create a free account on LG's webOS developer site.
2. On the TV, install the "Developer Mode" app from the LG Content Store, sign in, turn on Dev Mode Status and restart the TV.
3. Turn on Key Server in the Developer Mode app, then register the TV on your build machine with `ares-setup-device`. Name the device `tv`, since the npm scripts expect that name. Then fetch its key with `ares-novacom --device tv --getkey`.
4. Consider putting a parental app lock on the Developer Mode app so it can't be switched off.

### 6.3 Build and install the app

On the build machine (Windows, macOS or Linux, with Node.js 18+ and LG's `@webos-tools/cli`):

1. In `tv-app/quiz/`, copy `.env.local.example` to `.env.local` and set `LTP_QUIZ_URL` (the server address and port, for example `http://192.168.1.30:8765`) and `LTP_QUIZ_TOKEN` (the same token as in `QUIZ_TOKENS`). `.env.local` is ignored by git.
2. Run `npm ci`, then `npm run build`. The build rejects JavaScript syntax newer than ES2019, so it stays compatible with the TV's Chrome 79 engine.
3. Run `npm run package` to produce the `.ipk`, then `npm run install-tv` to install it.

The token is baked into the package, so anyone who gets the `.ipk` or the TV's filesystem can read it. The IP allowlist is the main line of defence. Rotate the token whenever you need to (see [operations.md](operations.md)).

### 6.4 Keep Developer Mode alive

A Developer Mode session lasts about 1,000 hours. When it runs out, every app installed through it disappears. The daemon renews the session once a day:

1. Copy the TV's private key fetched in step 6.2 to the server, for example `~/.ssh/ltp_tv_webos`. Remove its passphrase with `ssh-keygen -p` and make it readable only by you (`chmod 600`).
2. Connect to the TV once by hand (port 9922, user `prisoner`) so its host key lands in `known_hosts`.
3. Restart the service. Each day the daemon reads the session token from the TV over SSH and caches it. When less than 24 hours remain, it calls LG's renewal endpoint, sending only the session token. If renewal fails, or the Developer Mode or quiz app goes missing, you get one alert per day.

The key path, port, user and thresholds can be changed in the `devmode:` section of `settings.yaml`. The key path can also be set with `DEVMODE_SSH_KEY` in `.env`.
