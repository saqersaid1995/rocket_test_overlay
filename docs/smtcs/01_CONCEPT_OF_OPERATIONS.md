# 01 — Concept of Operations

## 1. Mission

SMTCS provides one time-correlated operational picture for a complete hazardous operation. It answers, in real time:

- What operation is being conducted?
- Which approved procedure and hardware configuration are in use?
- Who currently holds each authority?
- Is the site safe, are all stations ready, and which constraints remain open?
- Which sensor and video sources are healthy and recording?
- What command was requested, authorised, executed, or rejected?
- What happened, in what order, and what evidence proves it?

## 2. Supported operation classes

### 2.1 Static motor test

Phases:

```text
PLAN → CONFIGURE → REVIEW → SITE SETUP → CHECKOUT → SAFE
→ ARMING → ARMED → COUNTDOWN → FIRING → POST-FIRE
→ SAFE CONFIRMED → DATA REVIEW → CLOSED
```

Required operational objects:

- test request and objective;
- motor/test-article serial and configuration revision;
- propellant batch and manufacturing traveller references;
- stand and restraint configuration;
- approved test procedure revision;
- instrumentation manifest and calibration status;
- channel definitions and limit set;
- camera and recorder manifest;
- personnel roster and named authorities;
- exclusion-zone status;
- Test Readiness Review;
- executed procedure, event log, raw evidence, anomalies, result, and signed report.

### 2.2 Rocket launch

Phases:

```text
MISSION PLAN → VEHICLE INTEGRATION → CAMPAIGN → PAD SETUP
→ CHECKOUT → FLIGHT READY → LAUNCH READY → COUNTDOWN
→ LAUNCH → FLIGHT → RECOVERY → POST-FLIGHT REVIEW → CLOSED
```

Additional operational objects:

- mission objectives and measurable success criteria;
- released flight-vehicle configuration;
- payload and recovery configuration;
- launch site, range and airspace constraints;
- weather rule set;
- communications plan;
- launch and recovery team assignments;
- MDR, FRR, launch readiness review, and terminal Go/No-Go poll;
- flight events, recovery evidence, anomaly disposition, and mission report.

## 3. Operational principles

1. **Safety independence:** loss of SMTCS cannot cause ignition and cannot prevent physical safing.
2. **Positive control:** absence of data is never interpreted as GO.
3. **Single active authority:** every safety-critical role has one identified operator at a time.
4. **Two-person control:** defined hazardous actions require performer and verifier.
5. **Configuration pinning:** starting an operation freezes the procedure, limit set, channel map, and hardware configuration revision.
6. **Source quality:** every displayed measurement carries value, unit, source time, receive time, and quality.
7. **Fail explicit:** stale, invalid, disconnected, bypassed, inhibited, and unknown are distinct states.
8. **UTC event order:** every command, acknowledgement, sample marker, alarm, video marker, and operator action is time correlated.
9. **No silent override:** waivers and overrides require authority, reason, scope, and expiry.
10. **Evidence by construction:** the final operation package is assembled during execution, not recreated afterwards.

## 4. People and authority

| Role | Primary authority | Cannot do alone |
|---|---|---|
| Test Director | Conduct static-test sequence | Release an RSO safety hold |
| Launch Director | Conduct launch sequence | Override RSO or bypass firing interlocks |
| RSO / Range Safety | Establish range clear; impose/release safety hold | Execute firing command |
| LCO | Control physical SAFE/ARM and firing station | Declare range clear |
| Propulsion | Propulsion configuration and safing | Authorise overall operation |
| Avionics | Flight electronics and recorder readiness | Declare recovery/range ready |
| Instrumentation | DAQ, channels, calibration, recording | Accept failed safety limits |
| Ground Operations | Stand/pad, power, GSE, physical site | Approve flight configuration |
| Recovery Lead | Recovery readiness and field recovery | Release launch commit |
| Data Recorder | Video/data recording integrity | Declare vehicle safe |
| Procedure Controller | Procedure position and deviation record | Approve own deviation |

Administrative privileges do not grant operational authority.

## 5. Nominal static-test sequence

1. Test Director opens an authorised operation.
2. System verifies the pinned test article, procedure, channels, calibrations, personnel, and TRR result.
3. Ground Operations confirms physical stand and exclusion zone.
4. Instrumentation confirms channel health and recording pre-roll.
5. Video operator confirms every required view is online and recording.
6. Propulsion confirms test article configuration and igniter remains disconnected.
7. RSO declares range clear.
8. Procedure reaches igniter-connection step; two-person confirmation is recorded.
9. LCO physical station transitions SAFE to ARMED only when hardware interlocks are satisfied.
10. Test Director conducts station poll.
11. Countdown begins; any mandatory station may call HOLD according to the mission rules.
12. Firing is requested, authorised, and independently executed by the field control layer.
13. System records command request, hardware acceptance/rejection, firing event, telemetry, and video markers.
14. After pressure decay, Propulsion and RSO conduct safing and approach criteria.
15. Test Director transitions to POST-FIRE only after SAFE CONFIRMED.
16. Evidence is sealed, quick-look analysis is generated, anomalies are opened, and the report workflow begins.

## 6. Hold, abort, and emergency behaviour

### HOLD

- Freezes the procedure and countdown.
- Does not automatically change hardware state.
- Identifies originator, reason, affected criteria, and release authority.
- Requires an explicit release and, where necessary, a recycled procedure position.

### ABORT / SCRUB

- Ends the attempt and invokes the approved safing branch.
- Preserves all evidence and the exact procedure position.
- Requires a new attempt record; history is never overwritten.

### EMERGENCY

- Physical emergency actions take priority over software workflow.
- SMTCS records observed state and operator declarations but does not delay emergency response for data entry.
- Recovery to operations requires a deliberate incident and authority review.

## 7. Degraded modes

| Failure | Required response |
|---|---|
| Web client lost | Field control remains safe; operator reconnects; no implicit command retry |
| Mission server lost | Hardware retains safe state; active sequence holds; local recorders continue |
| DAQ link lost | Affected channels become DISCONNECTED; mandatory-channel rule determines HOLD |
| Sample becomes stale | Display freezes visibly with age; alarm raised; never shown as current |
| Camera lost | Recorder health alarm; mission rule determines whether view is mandatory |
| Time sync degraded | Alarm and uncertainty recorded; critical correlation criteria may force HOLD |
| Database unavailable | Commands rejected; field layer remains safe; append buffer retained locally if designed |
| Network partition | Edge systems record locally; no remote hazardous commands accepted |
| Recorder disk low/full | Predefined warning/critical alarms; critical recorder failure blocks commit |

