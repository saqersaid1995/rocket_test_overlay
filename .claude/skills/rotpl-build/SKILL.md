---
name: rotpl-build
description: Convert a finished ROTPL broadcast-overlay design (screenshot, HTML mockup, or description) into a real, validated, uploadable .rotpl template package for the rocket_test_overlay video compositor - reliably, without re-explaining the schema or hand-zipping files. Use this whenever the user brings back a finished template design and wants it turned into a working file, mentions ".rotpl"/"manifest.json"/"layout.json" for this app, asks what data/variables a template can bind to, asks "why won't my template activate," or wants design files packaged/uploaded into the app without manually zipping or hand-typing font hashes. Consult this before re-deriving ROTPL's schema from rotpl_registry.py/rotpl_renderer.py by hand or hand-building a .rotpl zip - the validated schema and a tested build/upload pipeline are already here. For producing the design brief that starts a new template (the step before this one), use the rotpl-brief skill instead.
---

# ROTPL template building

ROTPL is this repo's custom broadcast-overlay template format: a `.rotpl` file is a ZIP containing
`manifest.json` (template metadata + validation contract) + `layout.json` (the actual visual design, as
a list of positioned elements) + optional font/image files. The rendering engine is
`opencv-declarative-v1` — it draws each element with OpenCV/Pillow from the JSON description; there is
no HTML/CSS/JS inside a `.rotpl` file at all.

This skill exists so you never have to re-read `rotpl_registry.py` (validation rules) or
`rotpl_renderer.py` (drawing engine) from scratch, and never have to hand-zip a package and hope it's
right — the schema is captured in `references/`, and `scripts/` does the actual building/validating/
uploading against this repo's real code, not a re-implementation of it.

The user works in Arabic. Explain things to them in Arabic; the JSON/schema/code you produce stays in
English (it's literal syntax, not prose).

## Converting a finished design into a working `.rotpl` package

The user brings a design (screenshot, HTML, or just a description of what they want) — usually produced
via the **rotpl-brief** skill's design brief in an external tool like Claude Design. Turn it into a real
package:

1. **Read `references/layout-elements.md` first** — it has the exact field schema and a real working
   example for each of the 9 element kinds you're allowed to use (`text`, `logo`, `image`, `rect`,
   `line`, `gradient_scrim`, `vertical_gauge`, `bar_gauge`, `phase_list`). Map every visual element in
   the design to one of these — there is no generic shape/chart element, so a design element that
   doesn't fit one of the 9 needs to be approximated (e.g. a line chart isn't supported; a `bar_gauge`
   or a row of `text`/`rect` elements usually is what the design actually needed).
2. **Bind dynamic values to real paths from `references/runtime-bindings.md`** — every `bind`,
   `color_bind`, `opacity_bind`, `unit_suffix_bind` must be one of the exact dot-paths listed there
   (e.g. `telemetry.pressure.normalized`, `test.title`, `status.color`). A path that isn't in that list
   will just silently fail to resolve at render time.
3. **Author `manifest.json` and `layout.json` following `references/manifest-schema.md`** for the
   validation rules (id/version formats, canvas bounds, font declarations, `required_bindings` must
   each actually be used in the layout, etc.). Start from `assets/example-manifest.json` and
   `assets/example-layout.json` — they're a real, minimal, currently-passing pair; copy and extend
   rather than writing from a blank page.
4. **Package fonts correctly.** Every `font_id` a `text` element references must be declared in
   `manifest.fonts` with a real `.ttf`/`.otf` file in the archive (conventionally under `fonts/`). If
   you don't have real font files, ask the user for them rather than fabricating font bytes — a missing
   or invalid font blocks activation (draft install succeeds, activation doesn't).
5. **Build it with `scripts/build_rotpl.py`, never by hand-zipping.** Put `manifest.json`,
   `layout.json`, and any `fonts/`/asset files into one source directory, then run:
   ```
   python3 .claude/skills/rotpl-build/scripts/build_rotpl.py <source_dir> <output.rotpl>
   ```
   This computes each font's real `sha256`/`size_bytes` from the actual file bytes (a hand-typed hash
   is the single most common way a package silently fails to activate), zips the directory, and runs
   this repo's real `validate_rotpl()` against the result — so you get a genuine
   valid/activatable/errors report from the actual validator, not a guess. Exit code `0` means ready to
   activate, `2` means it installs but is blocked (see the printed `blocked_reasons`), `1` means it
   failed validation outright.
6. **Upload (and optionally activate) with `scripts/upload_rotpl.py`** against the running app:
   ```
   python3 .claude/skills/rotpl-build/scripts/upload_rotpl.py <output.rotpl> --activate
   ```
   (drop `--activate` to only install as a draft). Requires `python3 app.py` to already be running.
   This is more reliable than describing the upload to the user, since it exercises the actual
   `POST /api/templates` / `POST /api/templates/<id>/activate` endpoints and prints their real JSON
   responses. If the app isn't running, either start it first or hand the built `.rotpl` file to the
   user to upload themselves in the Design step of the web UI.
   To re-check an existing package later without rebuilding it (e.g. "why won't my template
   activate"), use `scripts/validate_rotpl_package.py <path.rotpl>` instead.

## The one gotcha that causes silent failures

The **upload validator** (`rotpl_registry.py`) accepts a slightly larger set of element types than the
**renderer** (`rotpl_renderer.py`) actually draws. `group`, `arc_gauge`, and `chart` pass upload
validation and install as a draft, but fail to load/render/activate at all. **Only use the 9 types
listed above** — never author `group`, `arc_gauge`, or `chart`, even though nothing stops you from
uploading a draft that contains them.

## Reference files

- `references/manifest-schema.md` — full `manifest.json` field-by-field validation rules, the ZIP
  package structure/safety limits, and the install → activate → rollback workflow.
- `references/layout-elements.md` — full `layout.json` schema, and the complete field table + a real
  working example for each of the 9 renderer-supported element types.
- `references/runtime-bindings.md` — the complete, exact list of dot-path values (`brand.*`, `test.*`,
  `camera.*`, `telemetry.*`, `metrics.*`, `frame.*`, `status.*`, `phases.*`) a `bind` field can
  reference, with type/default/availability notes for each.
- `assets/example-manifest.json` + `assets/example-layout.json` — a minimal, valid, currently-passing
  manifest/layout pair (mirrors `tests/test_rotpl_registry.py`'s fixture). Copy these as the starting
  point for any new package rather than authoring from scratch.
- `scripts/build_rotpl.py` — assembles a source directory into a validated `.rotpl`, auto-filling font
  hashes (step 5). All three scripts have been run end-to-end against a real local app instance
  (build → validate → upload → activate all succeeded) — trust them over hand-authoring.
- `scripts/upload_rotpl.py` — uploads to a running app and optionally activates in one step (step 6).
- `scripts/validate_rotpl_package.py` — re-validates an existing `.rotpl` without rebuilding it.
