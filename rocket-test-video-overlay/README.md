
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
- Upload Excel/CSV telemetry and auto-detect its columns.
- Configure synchronization, units, titles, resolution, logo, and audio.
- Follow render progress.
- Play and download the finished video.

Uploaded files and generated videos are stored under `workspace/`, which is
ignored by Git.

## Output

- Test video on the left.
- Pressure and thrust graph on the right.
- Moving time cursor and live points.
- Live pressure and thrust cards.
- Peak pressure and peak thrust summary.
- Original video audio preserved when FFmpeg is installed.
- 1920×1080 MP4 output by default.

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

Your Excel file needs three columns:

```text
Time (s) | Pressure (bar) | Thrust (N)
```

The column names can be different, but then specify them in `config.yaml`.

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

If telemetry starts at an absolute logger timestamp such as `44.56`, set:

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
