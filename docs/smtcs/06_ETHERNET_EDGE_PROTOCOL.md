# 06 — Ethernet Edge Protocol

## Decision

Static motor-test instrumentation uses wired Ethernet as the primary transport. The ESP/edge device initiates a persistent TCP connection to the SMTCS Edge Gateway on port 9100. The first implementation is inbound telemetry only and is not an ignition-control transport.

## Data policy

- Target acquisition: 1,000 samples/second for pressure and thrust, subject to ADC validation.
- Target batch: 50 samples every 50 ms.
- Every batch is application-acknowledged after durable database insertion.
- `device_id`, unique `boot_id`, monotonic `sequence`, first-sample microsecond time and sample period are mandatory.
- CRC32 protects application-frame integrity in addition to Ethernet/TCP checks.
- Sequence gaps and device reboot are explicit.
- The ESP must record the full raw stream locally to SD; network streaming is not the only evidence copy.
- The browser receives a decimated live view; it does not render every raw sample.

## Session

```text
ESP                          SMTCS Gateway
 |---- TCP connect :9100 -------->|
 |---- HELLO -------------------->|
 |<--- ACK / gateway UTC ---------|
 |---- BATCH seq=0 -------------->|
 |<--- ACK seq=0 -----------------|
 |---- BATCH seq=1 -------------->|
 |<--- ACK seq=1 -----------------|
 |---- HEARTBEAT ---------------->|
 |<--- ACK -----------------------|
```

## Batch example

```json
{
  "protocol": "SMTCS-EDGE/1",
  "type": "BATCH",
  "device_id": "ESP-DAQ-01",
  "boot_id": "unique-boot-uuid",
  "sequence": 182,
  "first_sample_us": 9100000,
  "sample_period_us": 1000,
  "sample_count": 50,
  "channels": {
    "pressure_raw": [0.03, 0.04],
    "thrust_raw": [0.1, 0.2],
    "temperature_raw": [28.2, 28.2]
  },
  "crc32": "calculated-over-canonical-frame"
}
```

The arrays in this abbreviated example must contain exactly `sample_count` values.

## Failure behaviour

- TCP loss: continue local SD recording and reconnect with the same boot ID.
- ESP reboot: generate a new boot ID; never hide the restart.
- Missing sequence: gateway increments the gap counter.
- Duplicate sequence: unique constraint prevents duplicate storage; ACK may still be returned.
- CRC failure: NACK and no storage.
- Oversize frame or batch: NACK and no storage.
- Gateway loss: ESP preserves buffered/local data and does not interpret loss as a control command.

## Commands

The first protocol version carries telemetry and health only. Hazardous control, SAFE/ARM, firing energy and emergency-stop functions remain outside this ESP telemetry link.

