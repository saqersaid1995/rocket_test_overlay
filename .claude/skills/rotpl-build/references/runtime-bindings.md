# Runtime binding namespace

Source of truth: `TemplateContext.from_runtime` in `rotpl_renderer.py`, called once per frame from
`rocket_overlay.py`'s render loop. This is the **complete and exact** list of dot-path strings a `bind`
/ `color_bind` / `opacity_bind` / `unit_suffix_bind` field can reference — nothing else resolves.

## Brand

| Path | Type | Notes |
|---|---|---|
| `brand.logo` | image | organization logo, or unavailable if none set |
| `brand.organization_name` | text | default `"STELLAR KINETICS"` |
| `brand.footer_tagline` | text | default `"STELLAR KINETICS — ENGINEERING THE VOID"` |
| `brand.accent_color` | color hex | default `"#F59E0B"` |

## Test identity

| Path | Type | Notes |
|---|---|---|
| `test.title` | text | default `"ROCKET MOTOR STATIC TEST"` |
| `test.subtitle` | text | default `"STATIC FIRE TEST"` |
| `test.run_number` | text | |
| `test.motor_type` | text | |
| `test.date` | text | |
| `test.site` | text | |
| `test.coordinates_text` | text | |
| `test.oxidizer` | text | |
| `test.fuel` | text | |
| `test.ablative_material` | text | |
| `camera.label` | text | default `"CAM 01"` |
| `camera.capture_fps_label` | text | default `"120 FPS"` |
| `units.pressure` | text | default `"bar"` |
| `units.thrust` | text | default `"N"` |

## Time & status

| Path | Type | Notes |
|---|---|---|
| `frame.mission_time_s` | float | seconds, can be negative pre-ignition |
| `frame.mission_clock` | text | formatted, e.g. `"T+ 00:04.20"` / `"T- 00:02.00"` |
| `status.code` | enum | `abort` / `pre_ignition` / `hot_fire` / `test_complete` |
| `status.label` | text | `ABORT` / `PRE-IGNITION` / `HOT FIRE` / `TEST COMPLETE` |
| `status.color` | color hex | `#FF4747` / `#94A3B8` / `#4ADE80` / `#FACC15` (matches the label above, in order) |
| `status.pulse` | float 0–1 | sinusoidal pulsing value, for animated status indicators |

## Live telemetry

| Path | Type | Notes |
|---|---|---|
| `telemetry.pressure.available` | bool | always `true` |
| `telemetry.pressure.value` | float | raw value |
| `telemetry.pressure.formatted` | text | e.g. `"15.5"` |
| `telemetry.pressure.unit` | text | e.g. `"bar"` |
| `telemetry.pressure.normalized` | 0–1 | **this is what gauges bind to**, not `.value` |
| `telemetry.pressure.scale_max` | float | `max(pressure_limit or 70.0, peak_pressure, 1.0)` — the value that maps to `normalized = 1.0` |
| `telemetry.thrust.available` | bool | **`false` for pressure-only tests** — any thrust-bound element should set `missing_policy` accordingly rather than assuming thrust always exists |
| `telemetry.thrust.value` | float or absent | |
| `telemetry.thrust.formatted` | text | formatted, or `"N/A"` when unavailable |
| `telemetry.thrust.unit` | text or absent | |
| `telemetry.thrust.normalized` | 0–1 or absent | |
| `telemetry.thrust.scale_max` | float or absent | |
| `telemetry.pressure.series` | list | `[{"time_s": float, "value": float}, ...]` — only present if a telemetry table was supplied |
| `telemetry.thrust.series` | list or absent | same shape, absent if thrust unavailable |

## Metrics (whole-test summary values)

| Path | Type | Notes |
|---|---|---|
| `metrics.pressure.peak` | float | peak pressure over the whole test |
| `metrics.pressure.peak_time_s` | float or absent | |
| `metrics.thrust.peak` | float or absent | |
| `metrics.thrust.peak_time_s` | float or absent | |
| `metrics.thrust.total_impulse` | float or absent | |
| `metrics.burn.duration_s` | float | |

## Mission phases

| Path | Type | Notes |
|---|---|---|
| `phases.active_id` | text | `auto_sequence` / `ignition` / `full_thrust` (aka `pressure_rise`) / `burnout` |
| `phases.active_index` | int | |
| `phases.progress` | float | always `0.0` — reserved, not currently meaningful |
| `phases.items` | list | see below — this is what `phase_list` elements bind to |

**`phases.items[]` row shape** (each item):

```json
{"id": "full_thrust", "label": "FULL THRUST", "time_s": 0.6, "time_text": "T+0.6s",
 "state": "active", "reached": true, "active": true, "available": true, "progress": 0.0}
```

The four phases in order: **AUTO SEQUENCE → IGNITION → FULL THRUST → BURNOUT**. `state` is one of
`pending` / `active` / `complete`.

## Automatic variable defaults

If `manifest.json`'s `variables.<path>` declares a `"default"`, that value is applied **only** when the
path above isn't otherwise available — it's a fallback, never an override. Declaring a default for a
path that's always available (like `units.pressure`) is harmless but has no effect.

## Required-bindings enforcement

Every path listed in `manifest.json`'s `required_bindings` is checked at render time; if any is
unavailable (even after defaults are applied), rendering fails with a binding error. Don't list a path
as required unless the design genuinely breaks without it.
