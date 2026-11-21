# Operations

All commands run on the server unless a step says otherwise.

## Everyday commands

- Run `bash deploy/deploy.sh status` to see the service status, the last log lines and today's usage.
- Run `journalctl --user -u ltp.service -f` to follow the live log.
- To deploy a new version, push or pull the new commits into the server's checkout, then run `bash deploy/deploy.sh`. It refuses to run if there are uncommitted changes in the checkout, so nothing local is overwritten. Stash or commit them first.
- After editing `server/config/settings.yaml`, run `systemctl --user restart ltp.service`.

## Rotating the quiz device token

The token in `QUIZ_TOKENS` (`server/.env`) must match `LTP_QUIZ_TOKEN` in `tv-app/quiz/.env.local`. If they differ, the app tells the child to ask a parent. To rotate it:

1. Generate a new token and put it in both files.
2. Rebuild and reinstall the app.
3. Restart the service.

## Troubleshooting the quiz app

| The app shows | Check |
|---|---|
| "Can't reach the server" | The service is running, `quiz.enabled` is true, the port is listening, and the firewall lets the TV's IP through. |
| "Please update the quiz app" | The app and the server disagree on the API version. Rebuild and reinstall the app. |
| "Ask a parent" | The tokens don't match. Follow the rotation steps above. |

If the bank fails to load, the port is taken or no token is configured, the quiz turns itself off and Telegram gets one alert per reason per day. Requests that were turned away are recorded as `quiz_denied` events, with the source IP and the reason but never the token.

## Developer Mode alerts

- **"Cannot read the Developer Mode token"**: the daemon couldn't reach the TV over SSH, and it has had no cached token for more than 24 hours. Check the key path and the `known_hosts` entry (see [setup.md](setup.md) §6.4).
- **"Renewal failed" or "less than 12 hours left"**: renew by hand from the Developer Mode app on the TV, then check the daemon's log.
- **"App missing"**: the Developer Mode app or the quiz app is no longer installed. The daemon doesn't reinstall them; reinstall from the build machine.

If the key on the TV changes, for example after Developer Mode is reset, fetch it again with `ares-novacom --device tv --getkey`, remove its passphrase, copy it to the server and connect once by hand.

## Database

- SQLite lives at `server/data/ltp.db`. On start-up the schema migrates forward on its own.
- Before upgrading across a schema change, stop the service and make a backup copy with a timestamp in its name.
- To roll back: stop the service, restore the backup, check out the matching older code, then start the service. Anything recorded after the backup is lost.
- To archive old data, rename the file with a timestamp. Don't delete it.

## Known limitations

- **The daemon can't act on a TV it can't reach.** If the child disconnects the TV from the network, the daemon can neither count nor enforce. It only sends a "connection lost" alert. A smart plug or a router-level block would be the real hard stop; neither is implemented.
- **Enforcement is reactive.** An app is closed about a second after it opens. Rapid reopening is contained by the "3 reopenings in 10 minutes means power-off" rule, not by blocking.
- **Standby behaviour varies.** On the tested TV, switching off drops the connection. TVs with "Quick Start+" may keep the connection while in standby; standby doesn't count as play time.
