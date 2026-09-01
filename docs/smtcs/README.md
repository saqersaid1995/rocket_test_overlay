# Stellar Mission & Test Control System (SMTCS)

Status: **Architecture review baseline — implementation is not authorised yet.**

SMTCS is the local ground-system platform for conducting Stellar Kinetics motor tests and rocket launch operations. It integrates procedures, operator authority, instrumentation, cameras, recording, event logging, readiness gates, and post-operation evidence while keeping hazardous control functions independent from the web application.

This package is the decision baseline that must be reviewed before further UI or application development:

1. [Concept of Operations](01_CONCEPT_OF_OPERATIONS.md)
2. [System Architecture](02_SYSTEM_ARCHITECTURE.md)
3. [Console and Workflow Specification](03_CONSOLES_AND_WORKFLOWS.md)
4. [Interface Control Catalogue](04_INTERFACE_CONTROL.md)
5. [Phase 1 Acceptance Plan](05_PHASE_1_ACCEPTANCE.md)

## Non-negotiable boundary

The browser is supervisory. It is not the sole safety barrier and does not directly energise an igniter. Physical safing, emergency stop, key control, interlocks, final firing authority, and safe-state behaviour remain in an independent field control layer.

