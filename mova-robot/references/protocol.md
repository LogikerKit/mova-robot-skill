# Protocol and evidence

Community implementation: https://github.com/ChuckleDucks/dreame-vacuum_z60
Pinned source: 521665082bb73a29354fa983b1feac356aa82c7b. MIT license retained.
The bundled protocol is a reduced subset; it excludes upstream device-discovery analytics.

Connection: local script → regional MOVA cloud HTTPS → robot. Uses a refresh token, not direct phone control. No public self-description/list-all-commands endpoint was established.

Observed on MOVA Z60 Ultra Roller Complete (`mova.vacuum.r9540h`), 2026-10-17:
- Properties: state 2.1, error 2.2, battery 3.1, charging 3.2, activity 4.1, minutes 4.2, area 4.3, suction 4.4, water 4.5, task status 4.7, cleaning parameters 4.10, grouped mode 4.23, custom cleaning 4.26, auto-switch settings 4.50, task type 4.58, map list 6.8.
- Reading 4.10 returned code -1; it did not reveal the start payload.
- Task type 15 confirmed a user-started spill task. Task type 16 denotes its paused counterpart in upstream definitions. These are status values, not established start commands.
- Mode uses packed bytes: low two bits 0 = vacuum+mop, 1 = mop, 2 = vacuum. Preserve remaining bits. Verified ordinary mop value in one configuration was 5121, but do not hard-code it.
- Water 1 is low/slightly dry in this integration; not necessarily the full native app scale, and not no-water spill cleaning.
- CleanGenius auto-switch `SmartHost=1` overrode a mop setting at task start. Setting only `SmartHost=0` and verifying it resolved this. It remains off after the helper completes.
- Actions: pause 2.2, stop 4.2, custom start 4.1. Custom start input properties: 1=18 (room cleaning), 10=JSON containing `selects:[[room,1,1,1,1]]`.
- After a room start, verify task status 3, activity 18, mode low bits 1 and error 0. Returning/washing states may be transitions; report uncertainty rather than claiming cleaning.
- State 4/activity 12 with error 88 occurred while the app displayed paused. Meaning of error 88 is unresolved; do not clear it blindly or permanently remap it to paused.

Room listing decodes only unencrypted saved maps. Select the entry whose binary map ID matches `curr_id`. Reject unsupported/encrypted maps. Type 6 denotes bathroom, but IDs vary by home. Never publish map images, names, device IDs or histories.

Spill-mode user demonstration: https://us.forum.mova-tech.com/forum.php?mod=viewthread&tid=348
Describes the blue-droplet Water Stain Cleaning option under room/zone cleaning, collecting water without adding fresh water. This is a community report, not a universal guarantee for every firmware or spill type.
