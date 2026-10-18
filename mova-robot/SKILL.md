---
name: mova-robot
description: Read MOVAhome robot status and rooms, pause or stop, mop a selected room, or open a private local MOVA Z60 camera viewer with experimental supervised steering. Spill-mode detection is supported but spill-mode activation is not.
---

# MOVA robot

Use `python3 scripts/mova.py` relative to this skill folder. Requires local Python 3.10+, requests, network access and a MOVAhome session. This is a local filesystem skill, not an account-wide ChatGPT connector. Do not upload local credentials to a cloud runtime to make it work.

## Connection and reads

Run `status` first. Credentials live outside this skill at `~/.config/mova-robot/session.json` (or `MOVA_CONFIG_DIR`), with owner-only permissions. Do not print, publish or include this file in artifacts. If missing/expired, launch `login` in a user-visible terminal; let the user enter their password privately. Never ask for the password in chat. Multiple devices require an explicit `--device` ID chosen from `devices`.

`devices`, `status`, and `rooms` are reads. Room maps are personal data; do not publish them. Room names and IDs must come from the current device map, never from examples or a previous user's map. For ambiguous room names ask which room.

## Authorized actions

Only run mutations for a user-requested action. Existing explicit requests suffice; do not add redundant approval questions. `--execute` expresses that authorization to the script.

- `pause --execute` pauses; `stop --execute` ends a task. Report the verified post-action status.
- `mop --room ID --execute` requests one pass of ordinary mop-only cleaning with the integration's low water setting (1). Supports only the tested model `mova.vacuum.r9540h`.
- If replacing an existing task is authorized by the user's request, add `--replace-task`. Explain that the previous task is ended. Do not resume unrelated tasks.
- The helper disables CleanGenius to prevent it from overriding mop-only mode. Disclose this persistent setting change. It verifies mop-only mode before and after starting. If the robot changes mode, it pauses and reports a mismatch. Do not blindly retry a start after a timeout or unknown outcome; read status first.
- No unrestricted raw-command interface is provided. Do not guess IDs or write status values to try to discover actions.

## Spill mode and interpretation

Read [references/protocol.md](references/protocol.md) for mappings and observed limitations. Task type 15 means water-stain cleaning; 16 means paused water-stain cleaning. The start command is NOT established. Never substitute ordinary low-water mopping for a requested spill/no-added-water mode. Explain this limit and let the user start it in MOVAhome, then verify `status`.

Readbacks can lag. An acknowledgement alone is not proof of execution. Unknown error codes should be reported as unknown; user-visible app information may be needed. Keep task type, cleaning mode, current activity and water setting distinct. Do not claim water level 1 proves that no water is added or that every Z60 firmware uses the same UI scale.

## Camera and supervised steering

Read [references/video.md](references/video.md) before using these experimental features. Camera support currently covers the European MOVA Z60 using Aliyun. Requires Node.js 22+, ffmpeg, an existing MOVAhome camera PIN, and one-time extraction from the user's MOVAhome APK. Never request a PIN in chat, inspect a filled PIN field, pass it as a command-line argument, or store it. Never set/reset the PIN or switch video vendors.

Run `python3 scripts/viewer.py --allow-steering` and open its private loopback link in the user's browser. Opening the page does not activate the camera. The user enters their PIN privately and clicks Open live view. The viewer requires native media encryption, attempts camera close after errors/cancellation, and limits each view to 60 seconds. It saves no footage. Distinguish video-login success, a start acknowledgement, and actual received frames; only the latter verifies a working view.

Steering is for a person with the robot in sight. The viewer sends one 250 ms requested pulse and an explicit stop; cloud delay can prolong physical motion. Firmware automatic stopping has not been verified. Do not automate navigation, infer a clear path from delayed video, or claim a stop acknowledgement proves a physical stop. No reverse preset is exposed: the app's lower arrow rotates rather than reversing. A failed or uncertain movement disables further viewer movement for that run. Stop remains available.

For an explicit supervised CLI request, use `steer --direction forward|left|right|stop --duration-ms 250 --experimental-steering --execute`. Nonzero motion requires an idle, undocked robot with no active task or fault, adequate battery, child lock off and navigation sensor raised. Do not end an existing cleaning task merely to enable steering. Never automatically retry movement after an unknown result. If stop is unconfirmed, tell the user to stop the robot in MOVAhome immediately. Camera and steering are deliberately absent from the MCP bridge until live behavior is validated; do not expose raw commands or PINs as model tools.
