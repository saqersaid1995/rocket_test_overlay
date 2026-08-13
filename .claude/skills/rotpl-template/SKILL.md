---
name: rotpl-template
description: Design, brief, build, validate, and upload custom ROTPL broadcast-overlay templates for the rocket_test_overlay video compositor. Use whenever the user wants to create/design/update a video overlay template for rocket motor static-fire test footage, wants a design brief for an external design tool (Claude Design or similar), mentions ".rotpl"/"manifest.json"/"layout.json" for this app, asks what data a template can bind to, or brings back a finished design that needs to become a working template package. Also use it for "why won't my template activate" or when the user wants design files packaged/uploaded into the app reliably without hand-zipping or hand-typing font hashes. Consult this before re-deriving ROTPL's schema from rotpl_registry.py/rotpl_renderer.py by hand or hand-building a .rotpl zip — the validated schema and a tested build/upload pipeline are already here.
---

# ROTPL template design & authoring

ROTPL is this repo's custom broadcast-overlay template format: a `.rotpl` file is a ZIP containing
`manifest.json` (template metadata + validation contract) + `layout.json` (the actual visual design,
as a list of positioned elements) + optional font/image files. The rendering engine is
`opencv-declarative-v1` — it draws each element with OpenCV/Pillow from the JSON description; there is
no HTML/CSS/JS inside a `.rotpl` file at all.

This skill exists so you never have to re-read `rotpl_registry.py` (validation rules) or
`rotpl_renderer.py` (drawing engine) from scratch to do template work — the schema those files enforce
is fully captured in `references/`, cross-checked against the actual source and a passing test fixture.

The user works in Arabic. Explain things to them in Arabic; the JSON/schema/code you produce stays in
English (it's literal syntax, not prose).

## The two things this skill does

### A. Produce a design brief for an external design tool (e.g. Claude Design)

Claude Design (or any similar tool) produces a **visual mockup** — HTML/CSS, an image, a Figma-like
layout. It cannot output a working `.rotpl` package directly; the gap between "a picture of an overlay"
and "a validated `layout.json` with exact pixel coordinates and binding paths" is what step B closes.

So step A's job is just to make sure the mockup uses **real data and real constraints**, so it doesn't
have to be reworked later. Hand the external tool `assets/design-brief.txt` as-is (read it, then paste
its contents into the conversation with that tool) — it states the canvas size, the full real data
catalog, and the 9 allowed element kinds. Regenerate it only if the user asks for something the current
brief doesn't cover (e.g. a different canvas size) — edit `assets/design-brief.txt` itself so it stays
the single reusable prompt, don't just improvise new wording inline.

If the user asks "what data can a template show," the data catalog in `assets/design-brief.txt` (also
detailed with types/defaults in `references/runtime-bindings.md`) is the complete, authoritative answer
— don't invent fields that aren't listed there.

### B. Convert a finished design into a working `.rotpl` package

Once the user brings back a design (screenshot, HTML, or just a description of what they want), build
the actual package:

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
   python3 .claude/skills/rotpl-template/scripts/build_rotpl.py <source_dir> <output.rotpl>
   ```
   This computes each font's real `sha256`/`size_bytes` from the actual file bytes (a hand-typed hash
   is the single most common way a package silently fails to activate), zips the directory, and runs
   this repo's real `validate_rotpl()` against the result — so you get a genuine
   valid/activatable/errors report from the actual validator, not a guess. Exit code `0` means ready to
   activate, `2` means it installs but is blocked (see the printed `blocked_reasons`), `1` means it
   failed validation outright.
6. **Upload (and optionally activate) with `scripts/upload_rotpl.py`** against the running app:
   ```
   python3 .claude/skills/rotpl-template/scripts/upload_rotpl.py <output.rotpl> --activate
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
- `assets/design-brief.txt` — the ready-to-paste prompt for an external design tool (see workflow A).
- `scripts/build_rotpl.py` — assembles a source directory into a validated `.rotpl`, auto-filling font
  hashes (see workflow B step 5). All three scripts have been run end-to-end against a real local app
  instance (build → validate → upload → activate all succeeded) — trust them over hand-authoring.
- `scripts/upload_rotpl.py` — uploads to a running app and optionally activates in one step (step 6).
- `scripts/validate_rotpl_package.py` — re-validates an existing `.rotpl` without rebuilding it.
