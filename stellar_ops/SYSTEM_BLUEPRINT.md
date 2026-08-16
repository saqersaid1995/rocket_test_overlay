# Stellar Kinetics Operations Platform

## System baseline — revision 0.1

### 1. Purpose

Stellar Ops is the authoritative operational record for the complete lifecycle of a spaceflight programme: concept, engineering configuration, manufacturing, ground test, launch campaign, flight, recovery, anomaly resolution, and close-out.

The platform is not a presentation dashboard. It is a controlled workflow system in which every operational decision is attributable, every vehicle and test article is configuration-controlled, and no safety-critical gate can be bypassed without an explicit, recorded waiver.

### 2. Isolation boundary

Rocket Overlay Studio remains an independent application and is protected by the following boundary:

- Existing `app.py`, `templates/index.html`, `static/app.js`, `static/app.css`, renderers, overlay templates, and Studio APIs are outside the Stellar Ops change set.
- Stellar Ops is implemented beneath the `stellar_ops/` namespace and runs as a separate service.
- Future integration is one-way through a versioned adapter: Stellar Ops may register a Studio export as evidence, but it does not own or mutate Studio rendering behaviour.
- CI must fail when an operations-only pull request changes a protected Studio path.

### 3. Authoritative hierarchy

```text
Portfolio
└── Programme
    ├── Mission
    │   ├── Launch campaign
    │   ├── Flight vehicle configuration
    │   ├── Payload
    │   └── Recovery operation
    ├── Vehicle family
    │   ├── Configuration baseline
    │   ├── Serialized assemblies
    │   └── Interface control documents
    ├── Test campaign
    │   ├── Test request
    │   ├── Test article configuration
    │   ├── Procedure and limits
    │   ├── Execution record
    │   └── Test report
    └── Risks, actions, decisions, documents and waivers
```

### 4. Core operational modules

#### 4.1 Programme control

Owns objectives, accountable manager, schedule milestones, budget reference, programme risks, mission portfolio, and lifecycle status. Programme closure requires all missions closed, open safety actions dispositioned, and records archived.

#### 4.2 Mission control

Owns mission requirements, success criteria, launch window, launch site, payload, flight profile, vehicle assignment, regulatory evidence, readiness gates, flight record, recovery, and post-flight review.

#### 4.3 Product and configuration management

Represents vehicle families, stages, propulsion units, avionics, structures, recovery systems, ground-support equipment, and payload interfaces. A configuration baseline is immutable after release. Changes are made through a configuration change request and create a new revision.

#### 4.4 Manufacturing and quality

Owns part masters, serialized parts, lots and batches, travellers, inspections, material certificates, non-conformance reports, rework, concessions, and acceptance status. A serialized assembly may only be installed when its status is `ACCEPTED` or covered by an approved concession.

#### 4.5 Test management

Supports propulsion static fire, hydrostatic pressure, avionics, telemetry, recovery, structural, environmental, integrated vehicle, and rehearsal tests. Each run pins the exact test article baseline, calibrated instruments, approved procedure revision, acceptance limits, personnel, raw evidence, anomalies, and disposition.

#### 4.6 Launch campaign operations

Owns campaign phases, range coordination, site readiness, weather constraints, notices and permits, team assignments, countdown procedure, communication checks, hazardous operations, holds, aborts, scrub/recycle, launch commit, and real-time event logging.

#### 4.7 Safety and mission assurance

Owns hazards, causes, consequences, controls, residual risk, verification evidence, risk acceptance authority, incidents, safety actions, waivers, and stop-work events. RSO stop authority is unconditional and recorded independently of schedule authority.

#### 4.8 Procedures and readiness

Procedures are revision-controlled templates. Executions create immutable step records with performer, verifier where required, UTC timestamp, measured value, evidence, and exceptions. TRR, FRR and launch Go/No-Go are gate instances with objective entry criteria and role-based approvals.

#### 4.9 Telemetry, data and evidence

Registers sensors, calibration validity, channels, units, sample sources, raw data packages, time correlation, derived products, video exports, reports and checksums. Large binaries live in object storage; the database stores metadata, relationships and integrity hashes.

#### 4.10 Recovery and post-mission

Tracks last known state, search zones, recovery teams, recovered hardware, chain of custody, photographs, safing, damage inspection, data retrieval, mission outcome, anomalies, corrective actions and lessons learned.

### 5. Lifecycle state machines

#### Programme

`DRAFT → APPROVED → ACTIVE → ON_HOLD → ACTIVE → COMPLETE → ARCHIVED`

#### Mission

`CONCEPT → PLANNING → INTEGRATION → CAMPAIGN → READY → COUNTDOWN → FLOWN | SCRUBBED | ABORTED → RECOVERY → REVIEW → CLOSED`

#### Test request

`DRAFT → TECHNICAL_REVIEW → SAFETY_REVIEW → AUTHORIZED → SCHEDULED → IN_PROGRESS → COMPLETE → ANALYSED → ACCEPTED | REJECTED → CLOSED`

#### Configuration item revision

`WORKING → IN_REVIEW → RELEASED → SUPERSEDED | WITHDRAWN`

#### Non-conformance

`OPEN → CONTAINED → INVESTIGATION → DISPOSITION_APPROVAL → REWORK | USE_AS_IS | SCRAP → VERIFIED → CLOSED`

Transitions are server-side rules. The UI never changes status directly; it requests a command, and the domain service validates authority, prerequisites, current state and concurrency version.

### 6. Readiness gates

Minimum gate set:

| Gate | Scope | Required authorities |
|---|---|---|
| Test Readiness Review | Each hazardous or integrated test | Test Director, Engineering, Safety, Quality |
| Mission Design Review | Mission baseline | Programme, Vehicle, Payload, Safety |
| Flight Readiness Review | Flight configuration | Launch Director, Chief Engineer, RSO, Quality |
| Launch Readiness Review | Campaign/site | Launch Director, RSO, LCO, Ground Ops, Recovery, Avionics, Propulsion |
| Go/No-Go Poll | Countdown | Named console roles; RSO has independent stop authority |
| Post-Flight Review | Mission closure | Programme, Engineering, Safety, Quality |

A gate result is `OPEN`, `GO`, `CONDITIONAL_GO`, `NO_GO`, or `SUPERSEDED`. Conditional approval must reference bounded conditions and an expiry. Missing approvals cannot be represented as approval.

### 7. Roles and separation of duties

Initial roles: System Administrator, Programme Manager, Chief Engineer, Configuration Manager, Quality, Safety Manager, RSO, Launch Director, LCO, Test Director, Propulsion, Avionics, Ground Operations, Recovery Lead, Data Engineer, Operator, Reviewer, and Read Only.

Key rules:

- Authors cannot approve their own released procedure or configuration revision.
- Safety acceptance requires a Safety authority distinct from the action owner.
- A launch commit requires all mandatory poll stations and cannot override an RSO hold.
- Administrative privilege does not imply operational approval authority.
- Records are archived, never silently deleted.

### 8. Audit and integrity

Every command writes an append-only audit event containing actor, acting role, UTC time, entity type and ID, action, reason, correlation ID, previous version, new version, and before/after hashes. Safety-critical approvals also retain the approved artefact revision and meaning of the signature.

Optimistic concurrency prevents two operators from unknowingly overwriting one another. All times are stored in UTC and displayed with an explicit timezone.

### 9. Technical architecture

- Modular monolith initially, with strict domain boundaries and versioned APIs.
- PostgreSQL for production; SQLite may be used only for isolated development tests.
- Object storage for raw telemetry, video, drawings and evidence.
- Background worker for reports, file validation, telemetry ingestion and notifications.
- Server-rendered operational views or a typed web client consume the same API; business rules stay in domain services.
- OpenAPI contract, database migrations, structured logs, health checks, backups and restore tests are mandatory production capabilities.
- Authentication through an organisation identity provider when deployed; local development identities are never production credentials.

### 10. Delivery sequence

1. Platform kernel: identity, roles, organisations, audit, documents, actions, decisions and state-machine engine.
2. Programme and mission control with requirements and milestones.
3. Product/configuration, serialized hardware, manufacturing and quality.
4. Test campaigns, procedures, instrumentation, execution and reports.
5. Launch campaign, readiness gates, countdown, holds and event log.
6. Telemetry catalogue, recovery, anomaly management and post-mission review.
7. Studio evidence adapter, production security, observability and disaster recovery.

Each phase is accepted only when its workflows, authorization rules, migrations, tests, operating documentation and failure behaviour are complete.

