---
target: SFT STELLAR web UI
total_score: 26
max_score: 40
na_heuristics: 
p0_count: 1
p1_count: 3
target_identity: "file:/home/claude/rocket_test_overlay/teststand/web/templates"
timestamp: 2026-10-30T00-18-05Z
slug: teststand-web-templates
---
Method: dual-agent (A: design review · B: detector + browser)

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of system status | 3 | Hardware/Firmware/Settings show no ignition state, T-minus or ABORT |
| 2 | Match real world | 3 | Engineer jargon: "bad frames", "gaps", firmware strings, "Receiver port" |
| 3 | User control | 3 | "Stop recording" enabled during COUNTDOWN; settings only "Save and restart" |
| 4 | Consistency | 2 | COUNTDOWN pill red vs orange block; "sim"/"simulation"/"Simulation, no output" |
| 5 | Error prevention | 3 | Arm checklist ignores live facts (camera offline, check FAIL, sim vs live relay) |
| 6 | Recognition | 3 | Chips clickable but not buttons; hold time not shown |
| 7 | Flexibility | 2 | Space hijacks every focused button; hold-to-start pointer-only |
| 8 | Aesthetic/minimal | 2 | Offline camera = largest object; pills duplicate chips; relay test beside ABORT |
| 9 | Error recovery | 3 | "REC FAILED" with no reason |
| 10 | Help | 2 | No pre-test procedure; calibration help buried |
| Total | | 26/40 | Acceptable |

Specificity: state colour system + Barlow Condensed authored; composition is a generic 2-col panel dashboard, equal chrome everywhere.
Detector: CLI 2 (broken-image FP settings.html:147, skipped-heading h1->h3 modal). Browser: console 7 (--faint #556273 timestamps 2.3-3.0:1, placeholder 3.0:1, ABORT hint 4.1:1, repeated-text FP, nested-cards FP), hardware 11 (FREE badge 4.4:1), settings 4 (INFO pill 4.4:1, heading), firmware clean.

Priority issues:
- [P0] Ignition state + ABORT + Esc missing on Hardware/Firmware/Settings -> harden, layout
- [P1] Arm modal lacks weight and live facts (link, pressure, cameras, SIM vs LIVE RELAY) -> clarify, harden
- [P1] Inverted hierarchy: offline camera dominates; pressure value smaller than SAFE; y-axis jumps to 0-100 on arm -> layout, bolder
- [P1] Keyboard/a11y: Space aborts on focused buttons, hold-to-start pointer-only, chips not focusable, modals no focus mgmt, --faint < 3:1 -> audit, harden
- [P2] Inconsistent status wording/colour, duplicate status rows, relay test next to ABORT -> clarify, distill

Personas: Alex (no shortcuts, log unfilterable, no session folder link), Sam (Space hijack, no focus trap, aria-live chatter every 100ms), Bay operator (12.5px muted text unreadable at 2-3 m, no full-screen FIRE/alarm cue, no post-test result).
Minor: log wrap indent, "Last abort: operator ABORT", invisible gauge, settings vs hardware alignment, primary button competition in camera card, upload disabled w/o reason.
Questions: Is pressure the one number at T-0? Should ARMED be a whole-UI mode? What is the end screen of a test?
