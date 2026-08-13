# layout.json schema & the 9 element types

Source of truth: `rotpl_renderer.py`. This is the file that defines what can actually be *drawn* — it's
stricter than what the upload validator (`rotpl_registry.py`) will merely *accept*. See the gotcha at
the bottom before authoring anything.

## Top-level `layout.json` keys

```json
{
  "schema": "rocket-overlay-layout",
  "canvas": {"width": 1920, "height": 1080},
  "layers": {"video": 0, "scrims": 10, "panels": 20, "fills": 30, "chrome": 40, "dynamic": 50, "brand_status": 60},
  "elements": [ /* 1-1000 element objects */ ]
}
```

- `schema` must equal `"rocket-overlay-layout"` exactly.
- `canvas` must numerically match `manifest.json`'s `canvas` (see `manifest-schema.md`).
- `layers` is a **naming convention only** — a non-empty object mapping arbitrary layer-name strings to
  z-index band values (-10000..10000), purely for the template author's own organization. It has no
  algorithmic effect; actual paint order is entirely driven by each element's own `z` field (elements
  draw in ascending `(z, array index)` order — higher `z` paints on top).
- `elements`: array of 1–1000 element objects, each with a unique `id` (case-insensitive, matches
  `^[A-Za-z][A-Za-z0-9_-]{0,127}$`).

## The gotcha: only 9 of the 12 "valid-looking" types actually render

- Upload validator accepts (12): `image, group, rect, line, gradient_scrim, text, logo, arc_gauge,
  bar_gauge, vertical_gauge, phase_list, chart`
- Renderer actually draws (9 — **use only these**): `gradient_scrim, rect, line, text, logo, image,
  vertical_gauge, bar_gauge, phase_list`

A package using `group`, `arc_gauge`, or `chart` **installs as a draft successfully** but fails to
load/render/activate with `Unsupported element type '<type>' in <id>.` — this is the single most common
way a template silently doesn't work. Never author these three.

## Common fields (every element)

| Field | Type | Notes |
|---|---|---|
| `id` | string | required, unique (case-insensitive) |
| `type` | string | required, one of the 9 types above |
| `z` | number | paint order, ascending; range -10000..10000 |
| `x`, `y` | number | position in **canvas-space pixels** (authored at the manifest's canvas size — e.g. 1920×1080 — scaled automatically to the actual output size) |
| `opacity` | number 0–1 | static opacity multiplier |
| `opacity_bind` | dot-path | if resolvable, overrides `opacity` |
| `color` | `#RRGGBB` or `#RRGGBBAA` | |
| `color_bind` | dot-path | if resolvable, overrides `color` |
| `bind` | dot-path | the runtime value this element displays/reacts to — must be an exact path from `runtime-bindings.md` |
| `missing_policy` | string | `hide` (default, element skipped) / `show_na` (text shows "N/A") / `show_fallback` (pairs with `fallback_element`) / `required_error` (hard render error) |
| `fallback_element` | element id | shown instead when this element's binding is unavailable |

## `gradient_scrim` — directional transparency gradient (for text legibility over video)

`x, y, w, h` (px) · `color`/`color_bind` (default `#FFFFFF`) · `opacity_start` (default 1.0) ·
`opacity_end` (default 0.0) · `direction`: `down` (default) / `up` / `left` / `right`

```json
{"id": "scrim_top", "type": "gradient_scrim", "z": 10, "x": 0, "y": 0, "w": 1920, "h": 190,
 "direction": "down", "color": "#05070A", "opacity_start": 0.72, "opacity_end": 0.0}
```

## `rect`

`x, y, w, h` · `fill`/`color`/`color_bind` (no fill drawn if none given) · `opacity`/`opacity_bind`
(default 1.0) · `stroke` (raw color, outline — not bindable) · `stroke_opacity` (default 1.0) ·
`stroke_width` (px, default 1) · `radius` (px, default 0 — >0 draws a rounded rect)

```json
{"id": "console_panel", "type": "rect", "z": 20, "x": 88, "y": 836, "w": 1744, "h": 150,
 "fill": "#080B10", "opacity": 0.58}
```

## `line`

`x, y` (start, required) · `x2, y2` (end, default = start) · `color`/`color_bind` (default `#FFFFFF`) ·
`opacity`/`opacity_bind` · `width` (px, default 1) · optional live-moving mode: `bind_type:
"translate_y"` + `bind` (value driving the motion) + `translate_map: {"domain_bind": "<dot-path to the
max/scale value>", "range_px": [y_at_0, y_at_1]}` — maps `bind / domain_bind` (as a 0–1 fraction)
linearly onto the `range_px` interval and shifts `y`/`y2` accordingly. Useful for a moving peak-value
tick mark on a gauge.

```json
{"id": "tape_peak_marker", "type": "line", "z": 35, "x": 1768, "y": 650, "x2": 1794, "y2": 650,
 "bind": "metrics.pressure.peak", "bind_type": "translate_y",
 "translate_map": {"domain_bind": "telemetry.pressure.scale_max", "range_px": [650, 310]},
 "color": "#FFFFFF", "width": 2}
```

## `text`

`text` (static literal string, required if `bind` absent) · `bind` (dynamic value; numbers formatted via
`decimals`, else `str()`) · `unit_suffix_bind` (dot-path; if resolvable and text isn't "N/A", appends
`" " + <resolved unit>"`) · `x, y` (required — **`y` is the text baseline**) · `anchor`: `left`
(default) / `right` / `center` · `font_id` (required, must match a declared `manifest.fonts[].font_id`)
· `font_size` (px, required, >0) · `min_font_size` (default = `font_size`; floor when `overflow:
"shrink"`) · `color`/`color_bind` · `opacity`/`opacity_bind` · `decimals` (0–10; formats a numeric bound
value as `f"{value:.{decimals}f}"`) · `letter_spacing` (px) · `max_width` (px, used with `overflow`) ·
`overflow`: `"shrink"` (reduce font size down to `min_font_size` to fit) or `"clip"` (hard-clip) ·
`missing_policy` / `fallback_element` as common fields.

```json
{"id": "lbl_mission_time", "type": "text", "z": 40, "text": "MISSION TIME", "x": 136, "y": 880,
 "font_id": "display-semibold", "font_size": 12, "anchor": "left", "letter_spacing": 4,
 "color": "#FFFFFF", "opacity": 0.5, "max_width": 340, "overflow": "clip", "min_font_size": 12}

{"id": "txt_org_wordmark", "type": "text", "z": 60, "bind": "brand.organization_name", "x": 88, "y": 84,
 "font_id": "display-bold", "font_size": 30, "anchor": "left", "letter_spacing": 2, "color": "#FFFFFF",
 "opacity": 1, "max_width": 320, "overflow": "shrink", "min_font_size": 22, "missing_policy": "hide"}
```

## `logo` / `image` (same drawing method)

`bind` (resolves to an image: a package-relative asset path, absolute path, raw bytes, PIL Image, or
numpy array) · `asset` (literal fallback image if `bind` is unavailable) · `x, y` · `max_width,
max_height` (px — image is scaled down to fit, preserving aspect ratio; falls back to `w`/`h` then the
image's native size) · `anchor`: `left` (default) / `right` / `center` · `opacity`/`opacity_bind`
(multiplies the image's own alpha) · `missing_policy` / `fallback_element`.

```json
{"id": "logo", "type": "logo", "z": 60, "bind": "brand.logo", "x": 88, "y": 44,
 "max_width": 300, "max_height": 54, "anchor": "left", "opacity": 0.97,
 "missing_policy": "show_fallback", "fallback_element": "txt_org_wordmark"}
```

## `vertical_gauge` / `bar_gauge` (same drawing method — do NOT use `arc_gauge`, it doesn't render)

`bind` (required — must resolve to a numeric fraction **0.0–1.0**, clamped; bind to an already-normalized
value like `telemetry.pressure.normalized`, never a raw value) · `x, y, w, h` (nothing draws if
`w<=0`/`h<=0`) · `color`/`color_bind` (fill color) · `opacity`/`opacity_bind` · `track_color` (raw color,
draws a full-box background track if present) · `track_opacity` · `fill_from`: for `bar_gauge`, `"left"`
(default, fills left→right) or `"right"` (fills right→left); for `vertical_gauge`, `"bottom"` (fills
bottom→up) or anything else (fills top→down) · `missing_policy`.

```json
{"id": "tape_fill", "type": "vertical_gauge", "z": 30, "x": 1774, "y": 310, "w": 14, "h": 340,
 "bind": "telemetry.pressure.normalized", "fill_from": "bottom",
 "color_bind": "brand.accent_color", "missing_policy": "hide"}

{"id": "pressure_bar", "type": "bar_gauge", "z": 30, "x": 564, "y": 948, "w": 170, "h": 5,
 "bind": "telemetry.pressure.normalized", "fill_from": "left", "color_bind": "brand.accent_color",
 "track_color": "#FFFFFF", "track_opacity": 0.15, "missing_policy": "hide"}
```

## `phase_list`

`bind` (required — resolves to `phases.items`, a list of phase-row objects, see `runtime-bindings.md`) ·
`x, y` (top-left) · `row_height` (px, default 30) · `max_rows` (1–1000, default = number of items) ·
`width` (px, default x-offset anchor for the `time_text` column) · `columns` (required object, four
fixed sub-objects — see below) · `missing_policy`.

**`columns` sub-schema** (each key optional):
- `columns.number` — row index text (`01`, `02`, ...): `x_offset`, `font_id`, `font_size` (default 12),
  `color_active_bind` (used when state is complete/active), `color_pending`
- `columns.diamond` — status marker: `x_offset`, `size` (px, default 10), `color_complete_bind`,
  `color_active`, `color_pending`
- `columns.label` — phase name text: `x_offset`, `font_id`, `font_size` (default 13),
  `letter_spacing`, `color_active`, `color_complete`, `color_pending`
- `columns.time_text` — the `T-`/`T+` time label: `anchor` (default `"right"`), `x_offset` (default =
  the element's `width`), `font_id`, `font_size` (default 12), `color_reached`, `color_pending`

```json
{"id": "phase_list", "type": "phase_list", "z": 50, "bind": "phases.items",
 "x": 1494, "y": 872, "row_height": 30, "max_rows": 4, "width": 298,
 "columns": {
   "number": {"x_offset": 0, "font_id": "mono-regular", "font_size": 12,
     "color_active_bind": "brand.accent_color", "color_pending": "#FFFFFF4D"},
   "diamond": {"x_offset": 44, "size": 10, "color_complete_bind": "brand.accent_color",
     "color_active": "#FFFFFF", "color_pending": "#FFFFFF2E"},
   "label": {"x_offset": 66, "font_id": "display-semibold", "font_size": 13, "letter_spacing": 3,
     "color_active": "#FFFFFF", "color_complete": "#FFFFFF8C", "color_pending": "#FFFFFF4D"},
   "time_text": {"anchor": "right", "x_offset": 298, "font_id": "mono-regular", "font_size": 12,
     "color_reached": "#FFFFFF99", "color_pending": "#FFFFFF40"}
 },
 "missing_policy": "hide"}
```

## How `bind` paths resolve

At render time, `bind`/`color_bind`/`opacity_bind`/`unit_suffix_bind` strings are looked up as **exact
flat keys** (e.g. `"telemetry.pressure.normalized"`) in the dict built once per frame — see
`runtime-bindings.md` for the complete list. A path not in that list simply never resolves (the element
falls back to `missing_policy` behavior) — there's no error thrown for typos, so double-check spelling
against the reference rather than guessing.

A binding is only treated as "available" if its value isn't missing/`None`/empty/non-finite, **and**
(for `telemetry.<channel>.*` or `metrics.<channel>.*` paths) `telemetry.<channel>.available` is also
true — this is how thrust-bound elements automatically hide themselves on pressure-only tests without
any per-element logic needed.
