---
name: mova-robot
description: Read MOVAhome robot status and room maps, pause or stop a robot, and run verified room-only mopping on a MOVA Z60 using a private local cloud session. Use for the user's MOVA robot; spill-mode detection is supported but spill-mode activation is not.
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
