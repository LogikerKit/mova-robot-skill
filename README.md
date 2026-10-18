# MOVA Robot skill and MCP connector (experimental)

A reusable local integration for MOVAhome robot status, room discovery, pause/stop and verified Z60 room-only mopping. Each owner signs into their own MOVAhome account; no device IDs, rooms or user credentials are bundled. Linux/Python 3.10+ and internet access are required. Not affiliated with MOVA.

Use the **skill** with a local agent, or the **MCP connector** with a compatible client. The connector can also be linked to personal ChatGPT through Secure MCP Tunnel. It is single-owner software, not a hosted multi-user service. [Connection and login-storage guide](docs/chatgpt.md).

## MCP connector

```bash
python3 -m venv .venv
.venv/bin/pip install -r bridge/requirements.txt
.venv/bin/python mova-robot/scripts/mova.py login
```

Configure your MCP client to run `.venv/bin/python bridge/server.py` using absolute paths. It exposes capability discovery, robot listing, status and rooms. Add `--allow-control` at startup to expose pause, stop and ordinary room mopping; mutation tools also require `execute=true` for a user-requested action.

The bridge uses stdio, opens no network listener, and never accepts login credentials through a model tool call. Every caller controls the same local account: keep its connection private to that owner. [ChatGPT setup instructions](docs/chatgpt.md#personal-chatgpt-connection-through-secure-mcp-tunnel).

## Install locally

Copy `mova-robot/` into `~/.codex/skills/` (or your supported client's skill directory). Install `mova-robot/requirements.txt` in your Python environment. Start a new chat or reload skills, then ask to use `$mova-robot`.

Sign in privately in a terminal:

```bash
python3 ~/.codex/skills/mova-robot/scripts/mova.py login
python3 ~/.codex/skills/mova-robot/scripts/mova.py status
python3 ~/.codex/skills/mova-robot/scripts/mova.py rooms
```

The login saves a refresh token, not your password, at `~/.config/mova-robot/session.json` with mode 0600, outside the skill. This is a plaintext file protected by owner-only permissions, not an encrypted vault. `MOVA_CONFIG_DIR` overrides this directory. Do not commit credentials, device IDs, maps or account output. To disconnect locally, stop the bridge/tunnel and remove the session file. Revoke server-side access in MOVAhome where supported; deleting a local file alone does not invalidate existing token copies.

## Supported scope

- Status and room listing; no room IDs are hard-coded.
- Pause and stop, with explicit `--execute` for authorized operations.
- `mop --room ID --execute`: one ordinary low-water mopping pass, currently restricted to `mova.vacuum.r9540h`. An existing task requires `--replace-task`. Disables CleanGenius and leaves it off. Reads back settings and pauses on mode mismatch.
- Spill-task detection using task type 15/16. **Starting dedicated spill cleaning is not implemented.** Low water is not no-added-water mode.
- Encrypted maps and other robot models need further validation.
- Experimental local video viewer and supervised steering for the European Z60. Existing camera PIN required; no recordings. Viewer controls are press-and-hold: release sends stop, with a five-second limit per hold and a short browser heartbeat lease. The CLI retains its bounded 1.25-second default. Live video and left/right turns with explicit stops have been observed on one Z60; forward movement and firmware automatic stopping remain unverified. [Setup, privacy and limitations](mova-robot/references/video.md). These features are local-only and are not exposed through MCP.

Installing the filesystem skill or publishing this repository does not automatically install a ChatGPT connection. The MCP connection must be configured separately and the host must be running. Importing instructions alone does not provide access to your computer, token or robot.

## Validation and provenance

Protocol subset derived from [ChuckleDucks/dreame-vacuum_z60](https://github.com/ChuckleDucks/dreame-vacuum_z60), commit `521665082bb73a29354fa983b1feac356aa82c7b`, MIT license retained in `mova-robot/LICENSE`. No discovery analytics included. New orchestration is experimental; offline tests cover credential permissions, map selection, mode mismatch and spill decoding. Live release checks are read-only; no robot movement is needed for installation.

Run the complete offline suite, including a real stdio MCP handshake and mocked robot operations, with `.venv/bin/python -m unittest discover -s tests`. The standalone skill tests need only `requests`: `python3 -m unittest discover -s tests -p test_mova.py`.

Camera authentication, encrypted media, worker, and browser-control tests use Node's built-in runner: `node --test tests/test_video_auth.mjs tests/test_video_media.mjs tests/test_viewer.mjs`. They use synthetic data and mocked browser events; they do not activate a camera or move a robot.
