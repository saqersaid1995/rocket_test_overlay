# 04 — Interface Control Catalogue

## 1. Interface classes

| ID | Interface | Direction | Criticality | First-phase disposition |
|---|---|---|---|---|
| IF-SEN-01 | Pressure DAQ | Field → SMTCS | Mission critical | Required |
| IF-SEN-02 | Load-cell DAQ | Field → SMTCS | Mission critical | Required |
| IF-SEN-03 | Temperature channels | Field → SMTCS | Mission critical | Required |
| IF-CTL-01 | Field controller state | Bidirectional request/status | Safety critical | Simulator first; hardware design separately reviewed |
| IF-CAM-01 | ONVIF camera discovery | Bidirectional management | Mission critical | Required |
| IF-VID-01 | RTSP video intake | Field → recorder/gateway | Mission critical | Required |
| IF-TIM-01 | NTP/PTP time | Time authority → all nodes | Mission critical | NTP required; PTP assessed |
| IF-LOG-01 | Existing logger import | File → SMTCS | Evidence | Required |
| IF-STU-01 | Rocket Overlay Studio export | Studio → operation evidence | Evidence | Later, isolated adapter |
| IF-FLT-01 | Flight telemetry | Vehicle → ground | Mission critical | Launch phase |

## 2. Device identity

Every device record includes:

- immutable device ID;
- human label;
- manufacturer/model/serial;
- firmware version;
- network or bus address;
- supported adapter and protocol;
- assigned operational function;
- calibration/maintenance status;
- permitted environment;
- expected sample/stream characteristics;
- criticality and loss response.

## 3. Channel definition

Every channel includes:

- stable channel ID;
- physical quantity and unit;
- source device/input;
- raw representation;
- engineering conversion revision;
- valid engineering range;
- expected update rate;
- stale timeout;
- warning and critical rules;
- whether mandatory for commit;
- recording rate and retention;
- display precision that does not exceed measurement certainty.

## 4. Camera acceptance fields

- registered ONVIF conformance/profile;
- RTSP stream URI resolved through protected credentials;
- main and substream codec/resolution/frame rate;
- lens/view assignment;
- power and network path;
- clock configuration;
- NVR target;
- required pre-roll duration;
- mandatory/optional status by operation phase;
- low-light and exposure configuration record;
- last health test.

ONVIF Profile T is the target for new cameras; compatibility must be verified against the vendor's registered product rather than assumed from marketing.

## 5. Command envelope

```json
{
  "command_id": "uuid",
  "operation_id": "OP-2026-001",
  "attempt_id": "ATT-003",
  "target": "FIELD-CONTROLLER-01",
  "requested_transition": "SAFE_TO_ARMED",
  "issued_at_utc": "timestamp",
  "expires_at_utc": "timestamp",
  "actor_id": "user-id",
  "acting_role": "LCO",
  "co_authority": "approval-id",
  "expected_controller_state": "SAFE",
  "correlation_id": "uuid"
}
```

Acknowledgement includes controller state before/after, accepted/rejected, rejection reason, controller timestamp, sequence, and health status. Transport and cryptographic design are selected during the field-controller safety review.

## 6. Integration discovery checklist

For every existing sensor, logger, camera, flight computer, or ignition controller, capture:

1. Manufacturer and exact model.
2. Electrical signal or network protocol.
3. Connector and physical interface.
4. Data format and update rate.
5. Time-stamping capability.
6. Configuration method.
7. Authentication/encryption support.
8. Local storage behaviour.
9. Failure and reconnect behaviour.
10. Available SDK/API/manual.
11. Calibration method.
12. Whether the device is observational or participates in a hazardous control.

