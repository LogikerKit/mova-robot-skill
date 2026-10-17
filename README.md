# MOVA Robot skill (experimental)

A local agent skill for MOVAhome robot status, room discovery, pause/stop and verified Z60 room-only mopping. Linux/Python 3.10+, `requests`, internet access and your own MOVAhome account are required. Not affiliated with MOVA.

## Install locally

Copy `mova-robot/` into `~/.codex/skills/` (or your supported client's skill directory). Install `mova-robot/requirements.txt` in your Python environment. Start a new chat or reload skills, then ask to use `$mova-robot`.

Sign in privately in a terminal:

```bash
python3 ~/.codex/skills/mova-robot/scripts/mova.py login
python3 ~/.codex/skills/mova-robot/scripts/mova.py status
python3 ~/.codex/skills/mova-robot/scripts/mova.py rooms
```

Credentials are stored at `~/.config/mova-robot/session.json` with mode 0600, outside the skill. `MOVA_CONFIG_DIR` overrides this directory. Do not commit credentials, device IDs, maps or account output. Revoke access by logging out/revoking sessions in MOVAhome and removing the local session file.

## Supported scope

- Status and room listing; no room IDs are hard-coded.
- Pause and stop, with explicit `--execute` for authorized operations.
- `mop --room ID --execute`: one ordinary low-water mopping pass, currently restricted to `mova.vacuum.r9540h`. An existing task requires `--replace-task`. Disables CleanGenius and leaves it off. Reads back settings and pauses on mode mismatch.
- Spill-task detection using task type 15/16. **Starting dedicated spill cleaning is not implemented.** Low water is not no-added-water mode.
- Encrypted maps and other robot models need further validation.

This is a filesystem skill. Installing it here does not install a ChatGPT workspace skill or provide a web/mobile connector. Importing the instruction bundle into ChatGPT alone does not provide access to your computer, token or robot.

## Validation and provenance

Protocol subset derived from [ChuckleDucks/dreame-vacuum_z60](https://github.com/ChuckleDucks/dreame-vacuum_z60), commit `521665082bb73a29354fa983b1feac356aa82c7b`, MIT license retained in `mova-robot/LICENSE`. No discovery analytics included. New orchestration is experimental; offline tests cover credential permissions, map selection, mode mismatch and spill decoding. Live release checks are read-only; no robot movement is needed for installation.

Run offline tests with `python3 -m unittest discover -s tests`.
