# 03 — Consoles and Workflows

## 1. Screen philosophy

SMTCS is role-oriented, not dashboard-oriented. Each console answers the decisions of one operator. Common layout:

```text
┌ Operation / Attempt / Mode / UTC / Environment / Authority ┐
├ State · T−/T+ · Active Hold · Critical Alarm · Recording ──┤
│ Primary role workspace                                     │
│                                                           │
├ Procedure position ────────┬ Event and alarm stream ──────┤
└ Command controls with prerequisites and acknowledgements ──┘
```

No colour is the only carrier of meaning. Every state has text and icon. Stale data shows its age. Hidden tabs cannot hide critical alarms.

## 2. Operations Director console

Purpose: maintain the total operational picture.

Required panels:

- operation identity, attempt, configuration, procedure revision;
- current lifecycle state and procedure position;
- T−/T+ clock plus UTC;
- station roster and voice-check status;
- readiness poll matrix;
- active holds and release authority;
- critical alarms and constraint violations;
- required camera mosaic;
- mission-critical channel strip;
- next five procedure actions;
- immutable event stream;
- commands: initiate poll, start/hold/resume countdown, declare abort/scrub, enter post-event phase.

Every command opens a confirmation sheet showing prerequisites, expected transition, required co-authority, and consequences.

## 3. Procedure console

Required capabilities:

- approved procedure revision and checksum;
- step types: instruction, confirmation, measured value, automatic criterion, timed wait, two-person verification, branching decision, command request;
- preconditions and postconditions;
- expected vs actual completion time;
- performer and verifier identity;
- attachments and photographs;
- deviation request without editing the active procedure;
- hold point and rollback/recycle target;
- resume after reconnect at the exact committed step;
- terminal summary of incomplete, skipped, failed, and waived steps.

## 4. Instrumentation console

Views:

- channel table for all sources;
- selected synchronized plots;
- device topology and link health;
- calibration validity;
- recording throughput and dropped samples;
- clock health;
- alarm/limit editor available only before operation pinning;
- zero/tare workflow with before/after evidence;
- controlled simulation/replay mode.

Plot rules:

- show physical unit and engineering range;
- show warning/critical limits;
- show gaps rather than interpolating missing safety data;
- identify raw versus filtered/derived values;
- allow event markers shared with video;
- preserve the original raw stream.

## 5. Video console

- 1/2/4/6/9 layouts;
- main/substream selection;
- camera identity burned into tile chrome;
- live latency and last-frame age;
- recording indicator independent from viewing indicator;
- pre-roll state;
- full-screen focus;
- operator event marker;
- instant review after operation without stopping evidence recording;
- synchronised replay with telemetry cursor.

## 6. Test Director console

- motor serial and configuration revision;
- propellant batch and traveller links;
- stand/restraint identity;
- approved limits summary;
- pressure, thrust and case-temperature plots;
- calculated but clearly labelled derived metrics;
- test elapsed time;
- ignition, first-rise, peak, burnout and decay markers;
- safing criteria;
- quick-look disposition: valid, partial, invalid, pass, fail;
- anomaly creation linked to time ranges and evidence.

## 7. Launch Director console

- mission and flight configuration;
- integrated countdown procedure;
- range, weather, vehicle, GSE, recovery and communications stations;
- constraint closure status;
- FRR/LRR status and unresolved exceptions;
- terminal poll;
- launch-window clock;
- ascent event timeline;
- recovery transition.

## 8. RSO console

- range/exclusion-zone status;
- airspace and road/access declarations;
- weather constraints relevant to safety;
- mandatory camera views;
- personnel accountability;
- active hazards and controls;
- independent HOLD control;
- release-hold workflow;
- emergency checklist;
- approach/safe declaration after event.

RSO status is independently sourced and cannot be represented as GO when disconnected.

## 9. LCO console

Display-only representations of physical/control states:

- local/remote mode;
- physical key state;
- SAFE/ARMED state;
- E-stop chain;
- interlock summary;
- igniter connection and continuity status;
- command inhibit reasons;
- accepted/rejected command acknowledgements;
- firing-event confirmation from field controller.

The UI does not fabricate continuity or armed state from operator input.

## 10. Recovery console

- team roster and comms;
- search sectors and tasking;
- last-known/predicted position when data exists;
- found/not-found timeline;
- safe-to-approach authority;
- photographs and hardware condition;
- chain of custody;
- recorder/data recovery;
- transition to post-flight inspection.

## 11. Alarm model

Alarm lifecycle:

```text
NORMAL → ACTIVE_UNACKNOWLEDGED → ACTIVE_ACKNOWLEDGED
→ RETURNED_UNACKNOWLEDGED → CLOSED
```

Alarm record contains source, criterion, measured value, limit, quality, first/last occurrence, count, acknowledgement, operational effect, and linked hold/anomaly.

Priorities:

- `P1 SAFETY`: immediate prominent alarm; defined automatic inhibit or required hold.
- `P2 MISSION`: threatens success or evidence validity.
- `P3 SYSTEM`: degraded capability requiring action.
- `P4 ADVISORY`: awareness; no immediate operational effect.

## 12. Countdown engine

Events may be time-based, condition-based, manual, or externally acknowledged. Each event defines:

- planned T-time;
- responsible station;
- prerequisites;
- command or procedure reference;
- timeout;
- failure branch;
- recycle point;
- evidence produced.

The clock does not advance procedural state by itself. A countdown event completes only when its completion criterion is recorded.

