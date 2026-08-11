
# Rocket Test Video Overlay

A professional Python tool for combining a rocket motor test video with synchronized Excel/CSV pressure and thrust telemetry.

## Web interface

The easiest way to run the tool is through its local Arabic web interface:

```bash
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000`. The interface lets you:

- Preview the source video before rendering.
- Synchronize up to three cameras by their individual ignition timestamps.
- Switch cameras at ignition, split the screen, or use picture-in-picture.
- Upload Excel/CSV telemetry, auto-detect its columns, and automatically locate
  the first sustained pressure rise even when the logger starts long before ignition.
- Configure synchronization, units, titles, resolution, logo, and audio.
- Switch live between the three selected supplied designs:
  `rocket_overlay_broadcast.py` (Launch), `(1).py` (Mission Control), and
  `rocket_overlay_broadcast-3).py` (Stellar Broadcast Console). The studio
  preview is rasterized by the same compositor used for export, so its layout
  does not drift from the selected Python template.
- Upload, validate, activate, and roll back declarative `.rotpl` design
  packages. The selected immutable template ID, version, and SHA-256 are pinned
  to both the preview and the export job.
- Follow render progress.
- Play and download the finished video.
- Select Engineering, Presentation, Archive, or Social output presets.
- Crop, zoom, enhance, denoise, and stabilize the source footage.
- Add test identity, safety limits, intro/outro cards, and a thumbnail.

Uploaded files and generated videos are stored under `workspace/`, which is
ignored by Git.

## Declarative ROTPL templates

The web studio accepts safe `.rotpl` packages from **قوالب ROTPL → رفع قالب
جديد**. A package is a ZIP container with `manifest.json` and `layout.json` at
its root, package-local PNG assets and TTF/OTF fonts, plus its reference images.
It cannot contain or execute Python, JavaScript, HTML, shell commands, network
resources, symlinks, or parent-directory paths.

Uploaded versions are immutable. The registry verifies archive limits, CRC,
paths, image/font signatures, declared font hashes, and every installed file
before a template reaches the renderer. A blocked draft remains visible with
its exact errors but cannot be selected for preview or export.

The completed built-in package produced for this project is:

```text
UPLOAD_TEMPLATE_HERE/stellar-kinetics-1.0.0.rotpl
```

It uses a `1920×1080` master canvas with embedded Archivo and IBM Plex Mono
fonts. The preview, SDR export, HDR graphics layer, and thumbnail all use the
same `RotplRenderer` and runtime bindings. Missing thrust remains unavailable
and is shown as `N/A`; it is never inferred from voltage.

## Output

- Adaptive landscape, portrait, and square layouts.
- Pressure-only mode that marks unavailable thrust readouts as `N/A` instead of
  presenting a measured zero.
- Progressively revealed graph with fill, live cursor, peaks, and safety limit.
- Live pressure and thrust cards.
- Six automatic test states, including safety-limit ABORT.
- Engineering summary with peak time, burn duration, average pressure,
  maximum dP/dt, and total impulse when thrust is available.
- Intro/outro cards, JSON summary, and downloadable thumbnail.
- Original video audio preserved when FFmpeg is installed.
- H.264 or H.265 at 540p, 720p, 1080p, 4K, square, or portrait sizes.
- Preview-matched SDR Rec.709 delivery is the default for consistent playback
  and social-platform publishing. Source resolution remains selected by default.
- Optional `--archive-hdr` mode keeps compatible HLG Main10 sources in HDR and
  composites shadows and graphics in linear BT.2020 light.

## 1. Install Python

Use Python 3.10 or newer.

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

For original audio, also install FFmpeg:

- macOS: `brew install ffmpeg`
- Ubuntu/Debian: `sudo apt install ffmpeg`
- Windows: install FFmpeg and add it to `PATH`

## 3. Prepare files

Create these folders:

```text
input/
output/
```

Put your files inside `input/`, for example:

```text
input/test_video.mp4
input/telemetry.xlsx
```

Your Excel file needs time and pressure columns; thrust is optional:

```text
Time (s) | Pressure (bar) | Thrust (N)
```

The column names can be different, but then specify them in `config.yaml`.

When thrust is unavailable, choose “غير متوفر” in the web interface or set
`thrust_column: __none__`. The supplied template geometry remains intact, while
every thrust value and peak is marked `N/A`; voltage is never mislabelled as thrust.

## Professional output controls

The web interface exposes all production controls. They can also be stored in
YAML:

```yaml
run_number: RUN-019
motor_type: L819
propellant: APCP
test_date: 2026-07-24
organization_name: STELLAR KINETICS
test_site: ETLAQ SPACEPORT - DUQM, OMAN
footer_tagline: STELLAR KINETICS - ENGINEERING THE VOID
camera_label: CAM 01
capture_fps: 120 FPS
output_style: engineering
broadcast_theme: stellar_console  # launch, mission_control, or stellar_console
intro_duration_s: 0
outro_duration_s: 2.5
reveal_chart: true
enhance_video: true
denoise_video: false
stabilize_video: false
crop_zoom: 1.35
crop_x: 0.62
crop_y: 0.55
pressure_limit: 55
delivery_codec: h264
preserve_source_quality: false  # true only for archival HLG Main10 output
thumbnail: true
```

Low-light enhancement is conservative to preserve flame detail. Denoising and
stabilization require more render time and should be enabled only when needed.

## 4. Configure synchronization

Copy the example config:

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml` and set:

```yaml
ignition_video_s: 4.52
```

This is the exact timestamp in the video at which ignition begins.

Set your Excel columns:

```yaml
time_column: Time (s)
pressure_column: Pressure (bar)
thrust_column: Thrust (N)
```

Leave `telemetry_zero_s: null` to detect the start of the sustained chamber
pressure response automatically. To override it with a known absolute logger
timestamp such as `44.56`, set:

```yaml
telemetry_zero_s: 44.56
```

Then telemetry `44.56 s` will line up with ignition in the video.

## 5. Run

```bash
python rocket_overlay.py --config config.yaml
```

The command-line interface remains available for automated workflows.

The finished video will be saved to:

```text
output/rocket_test_overlay.mp4
```

## Direct CLI example

```bash
python rocket_overlay.py \
  --video input/test_video.mp4 \
  --data input/telemetry.xlsx \
  --output output/result.mp4 \
  --time-column "Time (s)" \
  --pressure-column "Pressure (bar)" \
  --thrust-column "Thrust (N)" \
  --ignition-video-s 4.52 \
  --telemetry-zero-s 44.56 \
  --title "RNX-TEST-L819-V2"
```

## Millisecond telemetry

If the Excel time column is in milliseconds:

```bash
--time-scale 0.001
```

## Add a logo

Use a transparent PNG:

```yaml
logo: assets/logo.png
```

## Troubleshooting

### Graph is early or late

Adjust only:

```yaml
ignition_video_s: 4.52
```

Increase the value when the graph starts too early. Decrease it when the graph starts too late.

### Excel columns were not detected

Set the exact headings in `config.yaml`.

### Output has no audio

Install FFmpeg and ensure this works:

```bash
ffmpeg -version
```

### Video cannot be written

Keep:

```yaml
codec: mp4v
```

## GitHub repository structure

```text
rocket-test-video-overlay/
├── rocket_overlay.py
├── requirements.txt
├── config.example.yaml
├── README.md
├── .gitignore
├── input/
└── output/
```
