# Compatibility

## Tested

| | |
|---|---|
| TV | One LG model from the 2021 UP80 lineup |
| Platform | webOS 6.0 (sdkVersion 6.6.0), on two firmware versions; the TV updated itself while this was being built |
| Control library | bscpylgtv 0.5.5 (SSAP over `wss` on port 3001) |
| Quiz app engine | Chrome 79 (WebAppManager), 1920×1080 |

Other pre-2022 webOS TVs (webOS 4.x–6.x) use the same protocol and will probably work, but they're untested. On webOS 22 and later, LG's own Usage Limits feature may already cover what you need. If you try another model, please open an issue with the model, the webOS version and what worked.

## What was verified on the TV

**Control (SSAP)**

- **Pairing.** The first connection without a client key shows a prompt on the TV. That's why the daemon never pairs by itself; only `ltp pair` does.
- **Foreground-app pushes.** App switches are pushed within about a second. The adapter also polls the foreground app every tick as a liveness check.
- **Closing apps.** `system.launcher/close` returns 403 access denied. `com.webos.applicationManager/closeByAppId` works and drops back to the home screen in about a second, briefly reporting an empty foreground app on the way. The adapter uses `closeByAppId`.
- **Launching apps.** `system.launcher/launch` with `{"id": app_id}` brings the app to the front in about a second, including apps installed through Developer Mode.
- **Remote keys.** Key presses sent through the SSAP input socket reach the quiz app. This is used for automated end-to-end checks.
- **Power-off.** The TV switches off immediately and the WebSocket drops without reporting standby first. Once off, the TV is unreachable on the network, so a lost connection is treated as "off".
- **Wake-on-LAN power-on.** Port 3001 opens about 40 seconds after the magic packet, and the WebSocket accepts connections about 15 seconds after that. Connecting too early times out, and the daemon's reconnect backoff handles it. The TV comes back on its last input.
- **Not yet confirmed:** whether every toast is actually shown on screen, and what power state the TV reports when it's switched off with the physical remote.

**Library quirks (bscpylgtv 0.5.5)**

- `request()` and `subscribe()` add the `ssap://` prefix themselves, so the adapter passes bare luna paths.
- The library only sends keep-alive pings while it thinks the TV is on, so the `power` and `current_app` state subscriptions have to stay enabled.

**Quiz app (Developer Mode)**

- **Key codes.** Arrows are 37–40, OK is 13, Back is 461 and digits are 48–57. Yellow and blue are 405 and 406; red and green should be 403 and 404 but weren't confirmed on the TV. Holding an arrow repeats about a dozen times a second with the repeat flag set, so the app ignores repeats except for arrows. The Home key isn't delivered to the app.
- **Back key.** Without `"disableBackHistoryAPI": true` in `appinfo.json`, Back on the first screen opens the system "exit app?" dialog. With it, Back reaches the app as 461.
- **Pointer.** Shaking the Magic Remote sends 1536 and `cursorStateChange` when the pointer appears, and 1537 when it hides. The app ignores both, and every control also works by pointer click.
- **Lifecycle.** Pressing Home doesn't close the app; it only receives `visibilitychange` (hidden). Launching it again fires `webOSRelaunch` and keeps the page state. The first launch fires `webOSLaunch`.
- **Browser.** fetch, Promise and CSS Grid work. `:focus-visible`, `replaceChildren`, the `inset` shorthand and flex `gap` are unsupported, and the build and tests guard against them.
- **Developer Mode session.** The session token is in `/var/luna/preferences/devmode_enabled` on the TV, readable over SSH (port 9922, user `prisoner`). LG's `CheckDevModeSession.dev` and `ResetDevModeSession.dev` endpoints report the remaining time and reset it to 999 hours. Whether the token changes after a TV reboot wasn't tested, so the daemon re-reads it over SSH each time.

## Useful app ids

| App | id |
|---|---|
| YouTube | `youtube.leanback.v4` |
| Tubi | `com.tubitv.ott.tubi` |
| Home screen | `com.webos.app.home` |
| HDMI 1 input | `com.webos.app.hdmi1`. It counts as play time unless you exempt it; `closeByAppId` returns to the home screen. |
| Developer Mode | `com.palmdts.devmode` |
