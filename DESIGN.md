# SFT STELLAR design system: launch broadcast

The console is a live broadcast of our own test. The stand camera is the stage, telemetry sits over it, and a thin rail on the right holds the controls. There are no cards: areas are separated by hairline rules on a pure black ground.

Source of truth: `teststand/web/static/console.css` (tokens in `:root`), `settings.css`, `firmware.css`.

## Colour

Colour carries state only. Everything else is white at different opacities.

| Token | Value | Use |
|---|---|---|
| `--bg` | `#000` | every page ground |
| `--hair` / `--hair-2` / `--hair-3` | white 14% / 26% / 50% | rules, input borders, button outlines |
| `--text` / `--text-2` / `--muted` | white 100% / 78% / 62% | body, secondary, labels (all ≥ 4.5:1) |
| `--faint` | white 34% | dots and decoration only, never text |
| `--ok` | `#35d07f` | SAFE, live, in use, test complete |
| `--warn` | `#ffb21e` | ARMED, simulation, needs attention |
| `--fire` | `#ff6a2b` | COUNTDOWN, FIRING, live relay |
| `--bad` | `#ff4d55` | alarm, missing, failed |
| `--stop` | `#e5262f` | the ABORT button and nothing else |

## Type

All fonts are bundled in `static/fonts` (works offline).

- **Barlow 300–500:** UI text. 14px body.
- **Wide-tracked caps** (0.2–0.34em) at 11–12.5px: labels, nav and buttons.
- **Barlow Condensed 300:** numerals and state words.
  - Countdown: up to 112px.
  - Gauges: 60px.
  - Ignition state: 46px.
  - Page titles: 34px.
  - Values: 18–26px.
- **JetBrains Mono:** only for the firmware editor and log.

## Shape and components

- **Edges:** square everywhere (2px on controls), no shadows, no gradients. The one exception is a fade over the camera so the lower-third text stays readable.
- **Buttons:** outlined, uppercase and tracked.
  - Primary: white fill with black text.
  - ARM: amber outline.
  - Hold to start: orange outline with a fill that grows while held.
  - ABORT: solid `--stop` red, always visible on the console. Other pages show it in a state bar when not SAFE.
- **Status:** a dot plus a tracked label. In tables and events, the colour sits on the dot, not on the whole sentence.
- **Signature:** the test timeline under the countdown (READY, ARMED, COUNTDOWN, IGNITION, SAFED), plus 270° arc gauges for chamber and peak pressure laid over the stage.
- **Whole-UI state:** the top bar's bottom rule turns amber when ARMED and orange during COUNTDOWN and FIRING. FIRE and the pressure alarm draw a 4px frame around the screen plus a word plate; it never blocks clicks.

## Layout

- **Console:** top bar (56px), then a stage (fills the space) beside a rail (`clamp(330px, 22vw, 380px)`). The stage shows the camera, or the pressure trace when no camera is live; the operator can switch.
- **Other pages:** centred content up to 1120px. Settings has a sticky side nav.
- **Under 900px:** the order is ignition, then stage, then recording, then events.
