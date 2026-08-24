# Commercial Readiness Remediation Plan

Branch: `fix/commercial-readiness-10`
Baseline: `development/stellar-ops-v2`

## Definition of Done

No workstream is complete until all of the following are true:

1. Unit and integration tests pass.
2. Browser end-to-end tests pass against a fresh database.
3. No application JavaScript errors appear during the tested journey.
4. Keyboard and accessible-name checks pass for critical controls.
5. Workflow state, progress, release status and runtime context agree.
6. Failure, empty, loading, degraded and read-only states are verified.
7. The public Codespaces build is smoke-tested after deployment.
8. Evidence is recorded in the final acceptance matrix.

A visual improvement or a passing Python test alone is not completion.

## Workstream 0 — Baseline and Regression Ledger

- [x] Create isolated remediation branch.
- [x] Record live audit findings.
- [ ] Add regression tests for every confirmed defect.
- [ ] Establish repeatable seed database and browser test fixture.
- [ ] Add a single command for full validation.

Exit gate: the existing defects reproduce in tests before their fixes, except emergency P0 fixes which receive a regression test in the same workstream.

## Workstream 1 — Mission Control Reliability (P0)

- [x] Fix camera fullscreen binding JavaScript crash.
- [ ] Add browser smoke coverage for initial render and event binding.
- [ ] Verify SSE connect, disconnect and reconnect behaviour.
- [ ] Verify pop-outs, panel layout, alarms and incidents.
- [ ] Remove backup creation and system diagnostics from operational workspace.
- [ ] Replace native prompt/confirm interactions.

Exit gate: zero application console errors and all Mission Control controls initialise in Live, Pause and Replay modes.

## Workstream 2 — Canonical Lifecycle and State Model (P0)

Canonical order:

1. Operation Brief
2. Preparation Plan
3. Test Article
4. Configuration Baseline
5. Team & Authority
6. Procedure
7. Safety Assurance
8. Instrumentation
9. Video & Recording
10. Handbook & Execution Packs
11. Readiness Review
12. Crew Briefing
13. Rehearsal
14. Execution Release
15. Mission Control
16. Execution Close
17. Review & Corrective Actions
18. Evidence Package & Final Closure

- [ ] Define one authoritative stage and readiness model.
- [ ] Make progress derive from mandatory gates, not page count.
- [ ] Include preparation, safety, packs, briefing and closure evidence.
- [ ] Prevent contradictory COMPLETE, PENDING, CLOSED and NOT RELEASED states.
- [ ] Disable Mission Control entry without a current execution release.
- [ ] Invalidate downstream approvals when upstream controlled data changes.

Exit gate: no seeded or user-created operation can show a later stage as complete while a mandatory upstream gate is incomplete.

## Workstream 3 — Information Architecture (P1)

- [ ] Reorder lifecycle navigation to match dependencies.
- [ ] Move Handbook before readiness/briefing.
- [ ] Make Documents/Evidence and Change Control cross-cutting.
- [ ] Move backup, recovery and diagnostics to System Configuration.
- [ ] Remove the hidden conduct/execution surface from System Configuration.
- [ ] Keep all hazardous operational actions exclusively in Mission Control.

Exit gate: every capability has one clear owner surface and no hidden duplicate execution controls exist.

## Workstream 4 — Commercial UX and Accessibility (P1)

- [ ] Replace all native `prompt()` and `confirm()` calls.
- [ ] Add context-rich dialogs with validation, cancellation and preserved input.
- [ ] Label every input, select and textarea programmatically.
- [ ] Give icon-only controls accessible names.
- [ ] Add error summaries and focus the first invalid field.
- [ ] Improve secondary-text contrast and critical-control sizing.
- [ ] Break long forms into progressive sections without hiding gate status.
- [ ] Verify keyboard-only operation and focus trapping.

Exit gate: zero unlabeled form controls on critical pages and automated accessibility checks have no serious or critical findings.

## Workstream 5 — Operational Safety Boundaries (P0/P1)

- [ ] Enforce current release fingerprint at runtime.
- [ ] Require recording and telemetry quality where configured.
- [ ] Verify HOLD, RESUME, ABORT and safing guards.
- [ ] Make simulation, replay and live mode visually unmistakable.
- [ ] Prevent stale, expired or invalidated releases from commanding runtime.
- [ ] Verify command idempotency and audit-chain integrity.

Exit gate: every hazardous transition is denied by default and succeeds only with current evidence and explicit authority.

## Workstream 6 — Automated Quality Gates (P1)

- [ ] JavaScript syntax and unit tests.
- [ ] Browser E2E for the complete operation journey.
- [ ] Accessibility scanning.
- [ ] Broken-link and route coverage.
- [ ] Visual regression for primary desktop layouts.
- [ ] SSE/reconnect and degraded-mode tests.
- [ ] Concurrent update and idempotency tests.
- [ ] Backup/restore drill.
- [ ] CI publishes an acceptance report.

Exit gate: pull requests cannot merge when any mandatory gate fails.

## Workstream 7 — Full Acceptance Run

Test from a clean database:

- [ ] Create operation.
- [ ] Complete every controlled stage in canonical order.
- [ ] Prove each premature transition is blocked.
- [ ] Issue execution release.
- [ ] Open Mission Control and exercise simulation only.
- [ ] Close execution and complete review/evidence closure.
- [ ] Verify all statuses, percentages and audit records.
- [ ] Repeat in Codespaces public preview.

Exit gate: all acceptance evidence is green. Any remaining hardware-only validation is explicitly labelled and cannot be scored as tested without real hardware.

## Scoring Rule

A category receives 10/10 only when all required automated and live acceptance evidence is present. Untested hardware, security roles or production infrastructure is reported as not yet validated rather than awarded a speculative score.
