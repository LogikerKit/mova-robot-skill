# Experimental Z60 video and steering

The local viewer is implemented and covered by offline tests. European MOVA video authentication and the existing device-to-video binding have been checked live. On 2026-10-18 the owner confirmed that a live picture appeared in the local Chrome viewer on a Z60. Physical steering/stop behavior and automatic camera shutdown have not yet been verified live. Do not describe those as proven.

## Setup

Requires Linux, Python 3.10+ with this skill's requirements, Node.js 22+ and ffmpeg. First sign in using `scripts/mova.py login`, then run:

```sh
python3 scripts/extract_video_app_credentials.py /absolute/path/to/MOVAhome.xapk
python3 scripts/viewer.py --allow-steering
```

The extractor also accepts the base APK. It recognizes the production MOVA initialization pattern from version 1.6.7.609 and rejects unknown layouts. It does not execute the APK. The resulting `video-app.json` holds app-level integration credentials, separate from the owner's MOVA refresh token. Both live outside the skill in `~/.config/mova-robot/` with owner-only permissions. These are plaintext local files, not a keychain or encrypted vault. Do not upload or publish either file. The camera PIN and short-lived video tokens stay in memory/private process pipes, never command arguments or logs. Python/JavaScript cannot guarantee secure erasure of memory after use.

Open the private `127.0.0.1` link printed at startup on the same computer. Enter the existing camera PIN in the page. A session lasts at most 60 seconds after monitor start. Closing or hiding the page requests camera close; loss of viewer contact or video frames also closes the session. SIGINT/SIGTERM attempts cleanup. Hard process termination, power loss, or loss of connectivity can prevent the close request from arriving. The viewer reports whether close was acknowledged; an acknowledgement does not verify the camera's physical state.

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

Monitor session is MD5 of the logged-in account UID, underscore, and millisecond timestamp. Each robot write uses zero transport retries. Camera-end is attempted even if the start response is lost. Account locking is held only for individual cloud transactions, so a live view does not monopolize the login file.

Video uses MOVA `getAuthCodeV3`, Aliyun `loginbyoauth` and `createSessionByAuthCode`, then `vision/customer/stream/query` with `relayEncrypted:true`. The receiver requires the returned AES key and IV; it has no plaintext fallback. It applies the SDK's AES-128-CBC framing to AVC sequence headers/keyframes and audio. This is **selective media encryption over RTMP, not TLS**: RTMP control data, relay credentials, and video interframes are not covered by that AES layer. This follows the extracted SDK behavior; no TLS relay endpoint was established. [Aliyun's security documentation](https://help.aliyun.com/en/document_detail/2546058.html) recommends encrypted playback. Only H.264 is currently supported.

The video movement presets are forward `(200,0)`, left `(0,45)`, right `(0,-45)`, and stop `(0,0)`. The native lower-arrow preset `(0,180)` is a turn and is intentionally omitted. The unit scales and firmware deadman behavior are unverified. The CLI accepts 100–750 ms requested durations; this is a client deadline, **not a guarantee of movement duration**. Failed/slow network calls can delay stop. Use only under direct supervision and do not expose steering through the MCP bridge.

## Research provenance

Static inspection used REA 6.4.0 and JADX backend 0.7.1 on MOVAhome 1.6.7.609 (`com.dreame.movahome`). Base APK SHA-256: `b259536764f20f97a5ea2d89159086becebcc5d5467222ad942ad1e7fe927d09`.

The model-specific plugin was selected by MOVA's authenticated appplugin endpoint with model `mova.vacuum.r9540h`, appVer 152 and os 1. Version 1242 / model extension 128. Bundle MD5 matched the supplied metadata: `51de71a3e70316005bb98ff370306b46`; SHA-256 `5e81cd29e9c08756676163bfa05119d3c26564087f582b8dcffacc0e0bafd3e7`.

REA evidence: package `ev_506d672fb25e67af9f24916229fcea325882da658938a93fcb2989ba476c15c4`; application modules `ev_0980c07abab7ea976f6d1e3cf3e297fc07c733c6bb0fd05ca4566d9b0a8d5a1c`; framework version `ev_d59a90995db19d1e611632b2c1f6e3e6d4c9bc4ca94ccc675df3d2b3b5555ff0`. Static evidence establishes implementation intent, not runtime success.

Independent protocol findings came from Monitor, VideoRemoteView, RemoteView, VendorCheckUtil and StartUpPage; AES framing from liblvmedia.so. No vendor APK, proprietary decompiled code, native library, user identifiers or credentials are redistributed. The receiver/authentication transport adapts MIT-licensed `@apocaliss92/nodedreame` 1.12.4, with notices retained beside the corresponding code.
