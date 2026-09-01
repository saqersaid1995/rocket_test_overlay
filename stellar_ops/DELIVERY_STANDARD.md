# Delivery and acceptance standard

## Definition of done for every module

A module is not complete because its page renders. It is complete only when all of the following are true:

1. Domain terminology and accountable owner are documented.
2. State transitions and invalid transitions are enforced by the server.
3. Permissions are tested for allowed and denied roles.
4. Data changes are transactional and create audit events.
5. Concurrent updates are detected instead of silently overwritten.
6. Validation errors are explicit and preserve entered data.
7. Search, filter, export and archive behaviour are defined.
8. Empty, loading, failure, degraded and read-only states exist.
9. Database migrations have upgrade and restore procedures.
10. Unit, integration, authorization and lifecycle tests pass.
11. Operator guidance and administrator guidance are updated.
12. No protected Rocket Overlay Studio path changed.

## Phase acceptance

### Phase 1 — Platform kernel

- Organisation and user model
- Role assignments with scoped authority
- Append-only audit service
- Action, decision and evidence services
- State-machine command handler
- Authentication boundary
- Health and readiness endpoints
- Database migration runner

### Phase 2 — Programme and mission control

- Programme lifecycle
- Mission lifecycle and success criteria
- Requirements and verification matrix
- Milestones, dependencies, actions and decisions
- Mission configuration assignment

### Phase 3 — Product, manufacturing and quality

- Configuration item tree and released baselines
- Change requests and approval workflow
- Serialized assets, lots and installation history
- Manufacturing travellers and inspections
- Non-conformance and concession workflow

### Phase 4 — Test operations

- Test request and authorization
- Procedure revisions and step execution
- Instrument and calibration registry
- Test article configuration pinning
- Live event log, limit observations and holds
- Result review and signed report

### Phase 5 — Launch operations

- Campaign phases and team roster
- Site, range, weather and regulatory constraints
- TRR, FRR and launch readiness gates
- Countdown execution, holds, recycle and abort
- Role-based Go/No-Go poll and independent RSO stop

### Phase 6 — Flight, recovery and learning

- Telemetry package catalogue and integrity checks
- Recovery coordination and chain of custody
- Anomaly, corrective action and lessons learned
- Post-flight review and mission closure
- Studio export registration through an isolated adapter

## Release policy

Work is delivered through a dedicated branch and draft pull request. A phase cannot be merged until its acceptance checklist is demonstrably satisfied. Production deployment requires backup/restore verification, access review, secrets handling, structured logging, monitoring and an operator rollback procedure.
