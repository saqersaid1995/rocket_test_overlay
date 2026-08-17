# 05 — Phase 1: Static Motor Test Vertical Slice

## 1. Objective

Conduct one complete rehearsal and one authorised static motor test while SMTCS captures the procedure, authority, telemetry, cameras, command/status events, holds, evidence, and report. Phase 1 does not include direct browser control of live ignition hardware until the independent controller design has passed a separate safety review.

## 2. Included scope

### Operation configuration

- create operation and attempt;
- select motor/test article and configuration revision;
- pin procedure and limit-set revisions;
- assign Test Director, RSO, LCO, Propulsion, Instrumentation, Ground Ops, and Data Recorder;
- register required devices, channels, and cameras;
- conduct TRR and record approvals/exceptions.

### Live execution

- procedure step execution with performer/verifier;
- station readiness and Go/No-Go poll;
- T−/T+ clock, hold, resume, abort, and recycle;
- live pressure, thrust, and temperature with quality and limits;
- at least two synchronized IP-camera views;
- recording supervision;
- event and alarm stream;
- field-controller simulator acknowledgements;
- post-fire safing sequence.

### Post-operation

- raw telemetry export;
- video manifest;
- synchronized telemetry/video replay;
- event markers;
- anomaly creation;
- quick-look metrics;
- immutable attempt package and checksums;
- draft test report;
- Studio evidence handoff specification, without modifying Studio.

## 3. Required simulators before field use

- pressure source: nominal curve, over-limit, disconnect, stale and noisy modes;
- thrust source: nominal curve, dropout and saturation modes;
- temperature source;
- field controller: SAFE, ARM request accepted/rejected, watchdog, interlock-open, E-stop, late acknowledgement;
- cameras: live, delayed, disconnected and recorder-failed modes;
- time service offset and loss;
- storage warning/full condition.

## 4. Acceptance scenarios

| ID | Scenario | Pass condition |
|---|---|---|
| A01 | Nominal rehearsal | Complete lifecycle; sealed package produced |
| A02 | Pressure channel stale | Stale age visible; alarm raised; required hold enforced |
| A03 | Critical pressure limit | P1 alarm and configured inhibit/hold response recorded |
| A04 | Camera disconnected | Tile and recorder show separate failure; mission rule applied |
| A05 | Server/client reconnect | No duplicated command; procedure resumes at committed position |
| A06 | Field command rejected | Rejection reason visible; state remains unchanged |
| A07 | RSO hold | Countdown freezes; only authorised RSO release succeeds |
| A08 | Abort and safing | Abort branch executes; attempt preserved and cannot be resumed as nominal |
| A09 | Recorder disk critical | Commit blocked when recorder is mandatory |
| A10 | Time sync degraded | Uncertainty alarm recorded; configured criterion applied |
| A11 | Two-person step | Same user cannot satisfy performer and verifier roles |
| A12 | Evidence integrity | Recalculated hashes match the sealed manifest |

## 5. Performance targets to validate

Targets are validated against actual equipment before becoming requirements:

- live numerical display latency target: under 250 ms on the local network;
- operator video latency target: under 500 ms using the low-latency stream;
- event-log append acknowledgement target: under 100 ms;
- no silent telemetry loss;
- channel drop/gap detection within configured stale timeout;
- command requests are never automatically repeated;
- local recorders continue through mission-server interruption;
- recovery after client refresh without loss of committed procedure state.

## 6. Exit criteria

Phase 1 is complete only when:

1. all acceptance scenarios pass in simulation;
2. hardware interfaces are inventoried and reviewed;
3. hazard analysis identifies safety functions and boundaries;
4. a dry rehearsal is completed with the real operating team;
5. data/video timing uncertainty is measured;
6. backup and restore are demonstrated;
7. operator and emergency procedures are approved;
8. field use is explicitly authorised by the responsible safety authority;
9. Rocket Overlay Studio protected files remain unchanged.

## 7. Decisions required before implementation

- exact pressure sensor and DAQ/logger model;
- load cell and amplifier/DAQ model;
- temperature sensor/interface;
- existing ignition-controller design and electrical safety architecture;
- exact IP-camera models and number of required views;
- network distance and available fibre/copper infrastructure;
- control-room computers/displays;
- desired raw sample rates;
- recording duration and retention;
- named operating roles and approval authorities;
- site connectivity, power, UPS, and environmental conditions.
