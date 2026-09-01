# manifest.json schema & package rules

Source of truth: `rotpl_registry.py`. A `.rotpl` file is a plain ZIP (must start with bytes
`PK\x03\x04`). Two members are mandatory at the archive root: `manifest.json` and `layout.json`.

## Top-level manifest keys

| Key | Type | Required | Rule |
|---|---|---|---|
| `schema` | string | yes | must equal `"rocket-overlay-template"` exactly |
| `schema_version` | string | yes | semver `MAJOR.MINOR.PATCH[-prerelease][+build]`, max 32 chars |
| `id` | string | yes | matches `^[a-z0-9]+(?:[._-][a-z0-9]+)*$`, max 128 chars |
| `template_version` | string | yes | semver, max 64 chars |
| `name` | string or object | yes | plain string ≤200 chars, **or** `{"<lang-tag>": "<string ≤200 chars>"}` with at least one non-empty value; lang key matches `^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})?$` |
| `description` | string or object | no | not strictly validated |
| `canvas` | object | yes | see below |
| `entry` | string | yes | must exist in archive; for v1 packages must equal exactly `"layout.json"` |
| `engine` | object | no | if present, `engine.renderer` must equal `"opencv-declarative-v1"` |
| `fonts` | array | no (default `[]`) | ≤32 entries, see below |
| `font_fallback` | string | no | e.g. `"none"` — renderer honors strictly; missing/invalid declared font blocks activation |
| `variables` | object | no (default `{}`) | ≤256 entries, see below |
| `required_bindings` | array of strings | no (default `[]`) | ≤512 entries, each matches `BINDING_RE`, no duplicates, and **each one must actually be used** somewhere in `layout.json` (a `bind`/`*_bind` field) or validation fails |
| `optional_bindings` | array of strings | no (default `[]`) | same rules; cannot overlap `required_bindings` |
| `required_channels` | array of strings | no | ≤64 entries, each matches `ELEMENT_ID_RE`, no duplicates. In practice the only real channels are `"pressure"` and `"thrust"` |
| `optional_channels` | array of strings | no | same rules |
| `missing_data` | object | no (default `{}`) | maps a channel name → one of `hide`, `show_na`, `show_fallback`, `required_error` |
| `release_blocked_reason` | string | no | if truthy, package installs as a draft but is **not activatable** until removed |

`BINDING_RE = ^[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)*(?:\[\])?$`
`ELEMENT_ID_RE = ^[A-Za-z][A-Za-z0-9_-]{0,127}$`

## `canvas` object

Same shape/rules apply to `manifest.canvas` and `layout.json`'s top-level `canvas` — **the two must be
numerically identical** or validation fails with "The manifest and layout canvas dimensions must match."

- `width`: integer, **320–7680** (renderer additionally caps at ≤8192, requires >0)
- `height`: integer, **180–4320** (renderer additionally caps at ≤8192, requires >0)
- `aspect_ratio`: free-form string (e.g. `"16:9"`), not validated
- `units`: only `"px"` allowed (or omitted)
- `color_space`: only `"sRGB"` or `"Rec.709"` allowed (or omitted)
- `alpha_mode`: only `"straight"` allowed — enforced strictly by the renderer even if omitted

Standard broadcast canvas for this app: `1920 x 1080`, `aspect_ratio: "16:9"`, `units: "px"`,
`color_space: "sRGB"`, `alpha_mode: "straight"`.

## `fonts` array (max 32 entries)

| Field | Type | Required | Rule |
|---|---|---|---|
| `font_id` | string | yes | matches `ELEMENT_ID_RE`, unique case-insensitive, ≤64 chars |
| `file` | string | yes | archive-relative path, suffix `.ttf` or `.otf`. If missing from the archive → blocks activation (draft install still succeeds) |
| `family` | string | no | informational only, not validated |
| `weight` | number | no | informational only, not validated |
| `sha256` | string | recommended | if present, must match `^[0-9A-Fa-f]{64}$` and equal the actual SHA-256 of the file bytes, else `"Font sha256 mismatch"` error. If absent: only a warning |
| `size_bytes` | integer | no | if present, must equal the archive member's actual size, else error |

Font files live inside the ZIP, conventionally under `fonts/` (e.g. `fonts/Archivo-Bold.ttf`). At load
time the first 4 bytes must be a valid SFNT signature (`\x00\x01\x00\x00`, `OTTO`, `true`, or `ttcf`),
and the renderer actually instantiates the font via Pillow — an invalid font file fails hard, not
silently.

**Allowed archive file extensions overall:** `.json .png .jpg .jpeg .webp .ttf .otf .md .txt` — anything
else is rejected outright.

## `variables` object

Each key is a dot-path binding name (matches `BINDING_RE`, e.g. `"test.title"`,
`"brand.accent_color"`). Each value is an object:

- `type` (required): exactly one of `"text"`, `"number"`, `"color"`, `"image"`, `"boolean"`
- `required` (optional): boolean
- `default` (optional, any value): applied at render time via `context.set_default(path, default)`,
  **only** if that path isn't already available from the runtime data (see `runtime-bindings.md`) —
  it's a safety net, not an override.

Max 256 entries.

## ZIP package structure & safety limits

Required at archive root: `manifest.json`, `layout.json`.

| Limit | Value |
|---|---|
| Whole archive | ≤64 MB |
| Total uncompressed | ≤128 MB |
| Any single member | ≤32 MB |
| Total members | 1–256 |
| Compression ratio (zip-bomb guard) | ≤200× |
| Each JSON file | ≤2 MB, depth ≤32, ≤100,000 nodes |
| Image pixel count | ≤40,000,000 |
| Layout elements | 1–1000 |

**Allowed extensions:** `.json .png .jpg .jpeg .webp .ttf .otf .md .txt`
**Explicitly forbidden extensions:** `.py .pyc .pyo .js .mjs .cjs .html .htm .sh .bash .bat .cmd .ps1
.exe .dll .so .dylib .wasm .jar .class .php .rb .pl .lua .com .msi .scr .app .lnk .desktop`

Also forbidden: absolute paths, `.`/`..` path segments, NUL/control characters, backslashes, path
segments >128 chars, member names >240 chars, reserved Windows device names, case-insensitive/Unicode
filename collisions, symlinks or special files, encrypted members, any compression method other than
STORED/DEFLATED, and any path-traversal attempt at extraction.

JSON files are parsed with a hardened loader that rejects duplicate keys, non-finite numbers
(`NaN`/`Infinity`), and enforces the depth/node/key-length limits above. Any JSON key matching
`script, javascript, code, command, exec, execute, eval, shell, subprocess, plugin, module, import,
iframe, foreignobject, html, url, uri, href` (or an `on<event>`-style key), or any string value starting
with `javascript:`, `vbscript:`, `data:text/html`, or containing `<script`, is rejected anywhere in
`manifest.json`/`layout.json` — build layouts declaratively, never with embedded markup/script-like
content.

## Install → activate → rollback workflow

- `POST /api/templates` (multipart field name **must be** `template`, filename ending `.rotpl`) runs
  full validation, computes whole-file + per-member SHA-256, extracts into the registry, and installs
  as **`status: "draft"`**.
- A draft is **`activatable`** only if validation produced no errors **and** no `blocked_reasons`
  (e.g. a missing declared font, or an explicit `release_blocked_reason`).
- `POST /api/templates/<id>/activate` with `{"version": "<template_version>"}` flips the active
  pointer (previous active template is pushed onto a capped history).
- `POST /api/templates/rollback` restores the previous active template.
- Re-installing the exact same `id`+`version` with identical bytes is a no-op; with different bytes it's
  a `409` conflict — bump `template_version` for any real change.
