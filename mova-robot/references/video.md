# Experimental Z60 video and steering

Live video and supervised left and right turns followed by stopping were observed through fresh camera frames on a European MOVA Z60 on 2026-10-18. The isolated CLI left-turn test used a 1250 ms requested hold, with a second acknowledged direction command at 1000.1 ms and explicit stop dispatched at 1250.1 ms. Browser movement was disabled and the owner confirmed they were not using either controller. A subsequent right-turn test used the updated viewer button with the same requested hold and refresh pattern. Fresh frames before and after each test showed the corresponding rotation, then a steady scene; post-action status was idle with no error. Later owner-operated press-and-hold tests also observed forward movement and a right turn followed by stationary pictures, as described below. These observations validate those tests on this robot, not firmware automatic stopping or behavior during a lost connection. Camera close was acknowledged after testing, but physical camera shutdown remains unverified.

Earlier 250/750 ms one-shot commands were acknowledged without visible movement. Adding the SDK client identifier and preserving announcement state did not make the 250 ms tests move. The owner subsequently confirmed that holding the native app's direction moved the robot. The successful isolated held-command test supports replacing very short one-shot pulses with the app's one-second refresh pattern. The firmware's exact internal reason for ignoring the short pulses is unknown.

Those initial live tests preceded the press-and-hold interface. A later owner-operated Left hold and release was checked on 2026-10-18: fresh camera views before and after the gesture showed rotation, and two fresh frames sampled two seconds apart after release showed a stationary scene. The viewer reported stop acknowledged, with steering inactive and robot error 0. The robot's overall state readback was 23, which this helper does not currently label; it was not reported as idle. The owner supplied the sustained pointer input because the available browser automation could not hold a button. This observation confirms movement and subsequent stopping in that test, not exact motion onset, release-to-stop latency, or continuous movement throughout the hold. Browser gesture and lease edge cases remain covered separately by offline tests.

During that test Chrome's screenshot capture timed out. Actual camera images remained available through the existing authenticated loopback `GET /frame` endpoint. Freshness was checked with live `/status` responses and `frame_age_ms < 2000` immediately before and after each read. Images were inspected in memory without writing footage to disk. This restored camera observation independently of the failing browser screenshot facility; it did not repair Chrome capture or replace the real button interaction.

Further owner-operated tests on 2026-10-18 checked Forward and Right with roughly two-second holds. The Forward sequence showed translation after the steering handler finished; the camera session then expired. The owner confirmed the robot was stopped, and reopening the camera produced two stationary views two seconds apart. An initial Right observation was inconclusive because it watched only three seconds beyond the request and the owner subsequently moved the robot again. A separate Right hold with the owner leaving both controls untouched was observed for ten seconds after the request finished: the picture showed the turn, then four stationary views spanning six seconds. The viewer reported stop acknowledged. These observations support movement and subsequent stopping, not an exact release-to-stop latency or continuous motion for the entire physical hold.

Camera latency matters: `frame_age_ms` measures time since local JPEG decoding, not robot capture time. The receiver requests a relay `cacheDuration` of 3000 ms, and relay/decoding delay can make newly received frames depict earlier motion. This is not proof of a fixed three-second delay. A short observation window can therefore miss movement or mistake delayed pictures for continuing physical motion. Allow at least ten seconds of post-release observation with ample camera-session time remaining, and use the owner's direct observation to assess immediate stopping. No camera-delay assumption should authorize another movement after an uncertain stop.

## Setup

Requires Linux, Python 3.10+ with this skill's requirements, Node.js 22+ and ffmpeg. First sign in using `scripts/mova.py login`, then run:

```sh
python3 scripts/extract_video_app_credentials.py /absolute/path/to/MOVAhome.xapk
python3 scripts/viewer.py --allow-steering
```

The extractor also accepts the base APK. It recognizes the production MOVA initialization pattern from version 1.6.7.609 and rejects unknown layouts. It does not execute the APK. The resulting `video-app.json` holds app-level integration credentials, separate from the owner's MOVA refresh token. Both live outside the skill in `~/.config/mova-robot/` with owner-only permissions. These are plaintext local files, not a keychain or encrypted vault. Do not upload or publish either file. The camera PIN and short-lived video tokens stay in memory/private process pipes, never command arguments or logs. Python/JavaScript cannot guarantee secure erasure of memory after use.

Open the private `127.0.0.1` link printed at startup on the same computer. Enter the existing camera PIN in the page. A session lasts at most five minutes after monitor start. Switching away requests movement stop and clears the displayed picture; status heartbeats retain the already-open camera session so returning from another tab or chat does not require another login. No pictures are fetched while hidden, and a newly decoded, current picture is required before another hold. Closing the page or choosing Close camera requests camera close. Loss of viewer contact for 15 seconds or loss of video frames also closes the session. SIGINT/SIGTERM attempts cleanup. Hard process termination, power loss, or loss of connectivity can prevent the close request from arriving. The viewer reports whether close was acknowledged; an acknowledgement does not verify the camera's physical state.

The private viewer capability is kept in this tab's `sessionStorage` so refreshing the page retains access. The URL fragment is removed after reading it. This capability is separate from the camera PIN, which is never stored. An invalid capability is cleared, and the page explains that a fresh private viewer link is needed. Movement buttons explain their prerequisites: a current live picture, enabled steering, and a person watching the robot.

The camera PIN is checked once per user click. There is no automatic retry after a wrong code, no PIN-reset operation, no audio/intercom, no recording, no vendor switching, and no hosted storage. The page listens only on loopback, validates Host/Origin, and requires an unpredictable session token for every data or control request. Do not expose the listener through a tunnel: all callers would control this one owner's robot. This feature is not available in the Android ChatGPT app when the computer is off.

## Commands established from the Z60 plugin

| Operation | Service/action/property | Payload |
| --- | --- | --- |
| Verify existing PIN | 10001 / 4 / 1102 | `session`, `oldcode=SHA256(PIN)`; nested code 2 confirms verification |
| Initialize existing video vendor | 10001 / 7 / 11 | `session`; wait for vendor `ali`, initStatus 1 |
| Start camera monitor | 10001 / 1 / 1 | `session`, operType `monitor`, operation `start`, token `alify`, current channelId, EU area `4` |
| Keep monitor alive | 10001 / 1 / 6 | `session`, operType `keep_alive`, videoStatus `preparing` or `opened` |
| End monitor | 10001 / 1 / 1 | `session`, operType `monitor`, operation `end` |
| Manual motion | property 4 / 15 | JSON containing `spdv`, `spdw`, string `audio`, `random`, millisecond `timestamp` |

The cloud command envelope includes `data.from`, a stable local client identifier. The MOVA Android SDK generates `A` plus six hexadecimal characters and reuses it across commands. This implementation keeps its own identifier in the owner-only `~/.config/mova-robot/client-id` file, separate from login credentials; camera and steering use the same identifier. Replies with a nonzero outer code, an error, or a contradictory request ID are not accepted as acknowledgements.

Monitor session is MD5 of the logged-in account UID, underscore, and millisecond timestamp. Each robot write uses zero transport retries. Camera-end is attempted even if the start response is lost. Account locking is held only for individual cloud transactions, so a live view does not monopolize the login file.

Video uses MOVA `getAuthCodeV3`, Aliyun `loginbyoauth` and `createSessionByAuthCode`, then `vision/customer/stream/query` with `relayEncrypted:true`. The receiver requires the returned AES key and IV; it has no plaintext fallback. It applies the SDK's AES-128-CBC framing to AVC sequence headers/keyframes and audio. This is **selective media encryption over RTMP, not TLS**: RTMP control data, relay credentials, and video interframes are not covered by that AES layer. This follows the extracted SDK behavior; no TLS relay endpoint was established. [Aliyun's security documentation](https://help.aliyun.com/en/document_detail/2546058.html) recommends encrypted playback. Only H.264 is currently supported.

The video movement presets are forward `(200,0)`, left `(0,45)`, right `(0,-45)`, and stop `(0,0)`. The native lower-arrow preset `(0,180)` is a turn and is intentionally omitted. The unit scales and firmware deadman behavior are unverified. The CLI accepts 100–1250 ms requested durations and defaults to 1250 ms; holds above 1000 ms send at most one scheduled refresh after acknowledgement.

Viewer buttons use press-and-hold with pointer or Space/Enter keyboard input. Release, pointer cancellation, loss of focus, unchecking supervision, or loss of a current picture requests stop. Each hold has a five-second maximum and a one-second server lease renewed by browser heartbeats every 250 ms. The server checks cancellation, the lease, and video freshness before movement and each native one-second refresh. A release received before its corresponding start prevents that delayed start; completed hold identifiers cannot be reused. Normal release allows another deliberate hold. An uncertain movement acknowledgement disables further movement for that viewer run. Every attempted movement uses zero transport retries and an explicit stop in a finally block. These are client deadlines, **not guarantees of physical movement duration**: failed or slow network calls can delay stop. Use only under direct supervision and do not expose steering through the MCP bridge.

The initial movement in each camera session uses the native announcement flag; later pulses keep it off, including after an explicit stop. The controller remains alive for that camera session and is reset when a new live view starts. This matches app behavior but is not, by itself, proof of physical movement.

Navigation sensor property 28.4 is 0 when lowered and 1 when raised. The native RemoteView blocks lowered-sensor control only during a cleaning/task workflow outside remote control; it permits idle manual movement. Our stricter idle/no-task gates therefore accept either known sensor value, while rejecting missing, noninteger or other values. No automatic sensor-height change is sent.

## Research provenance

Static inspection used REA 6.4.0 and JADX backend 0.7.1 on MOVAhome 1.6.7.609 (`com.dreame.movahome`). Base APK SHA-256: `b259536764f20f97a5ea2d89159086becebcc5d5467222ad942ad1e7fe927d09`.

The model-specific plugin was selected by MOVA's authenticated appplugin endpoint with model `mova.vacuum.r9540h`, appVer 152 and os 1. Version 1242 / model extension 128. Bundle MD5 matched the supplied metadata: `51de71a3e70316005bb98ff370306b46`; SHA-256 `5e81cd29e9c08756676163bfa05119d3c26564087f582b8dcffacc0e0bafd3e7`.

REA evidence: package `ev_506d672fb25e67af9f24916229fcea325882da658938a93fcb2989ba476c15c4`; application modules `ev_0980c07abab7ea976f6d1e3cf3e297fc07c733c6bb0fd05ca4566d9b0a8d5a1c`; framework version `ev_d59a90995db19d1e611632b2c1f6e3e6d4c9bc4ca94ccc675df3d2b3b5555ff0`. Static evidence establishes implementation intent, not runtime success.

Independent protocol findings came from Monitor, VideoRemoteView, RemoteView, VendorCheckUtil and StartUpPage; AES framing from liblvmedia.so. No vendor APK, proprietary decompiled code, native library, user identifiers or credentials are redistributed. The receiver/authentication transport adapts MIT-licensed `@apocaliss92/nodedreame` 1.12.4, with notices retained beside the corresponding code.

The MOVA React Native SDK version 53 was fetched from the authenticated SDK-upgrade metadata endpoint for appVer 152. The extracted `sdk.bundle` matched the supplied MD5 `541ecc90deed4fe530819822e1772426`; SHA-256 `51ff80d69c402a90368e6e4f63bbeab3c821caea4eb5267d5eae79a7aa465319`. Its `IDeviceWifi.callMethod` establishes the `data.from` field and its persistent identifier format. SDK source is not redistributed.
