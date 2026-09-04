# ipod-movie-maker

Interactive CLI that turns a URL **or** a local file into an `.m4v` an **iPod touch
(5th gen)** — the default target — decodes in hardware. An iPod touch 7 is
supported too, as an explicit opt-in (`--device touch7`). Device-specific
sibling of `anydl` (universal downloader) and the archived `ipod-drop` (audio →
iTunes M4A).

**This repo is public.** Never commit anything that identifies what gets watched
or owned: no video/show/film titles, no video or playlist IDs, no playlist names
or counts, no rightsholder names, no raw tracebacks, no home directory paths, no
email addresses. Sample output in the docs uses invented neutral filenames
(`sample-clip.mkv`) and `~/…` paths. Getting this right *before* a commit is the
only way that works — git history keeps whatever was pushed once.

## Stack

- Python 3.8+, single-file script (`ipod_movie_maker.py`), stdlib only apart from yt-dlp
- `yt-dlp` (auto-installs on first run) — download side, plus the cookie retry
- External: `ffmpeg` / `ffprobe` — required, does all the muxing and encoding
- macOS is the assumed platform: `--fast` uses VideoToolbox, and the sync path
  (Apple TV app → Finder → device) exists nowhere else.

## Layout & entry points

- `ipod_movie_maker.py` — the entire tool. Key pieces:
  - `DEVICES` — **the single place device facts live.** Keyed by device id
    (`touch5` default = A5 / iOS 9.3.5 / H.264 Main level 3.1 / no HEVC;
    `touch7` = A10 Fusion / H.264 High level 4.2 / HEVC incl. Main10). Every
    accept/reject decision (`video_fits`/`audio_fits`/`verify`) and every
    encoder-arg builder reads `DEVICES[device_id]` — never hardcode a codec,
    profile, level, or fps limit anywhere else in this file. Adding a third
    device means adding one entry here, nothing more.
  - `PROFILES` — the four quality ladders (`sharp`/`standard`/`compact`/`tiny`).
  - `video_fits()` / `audio_fits()` → `build_plan()` — pick `copy` / `audio` /
    `encode` and record *why*, which is what the console report prints.
  - `build_ffmpeg_cmd()` — one builder for all three plans.
  - `verify()` — ffprobes the finished file against the envelope; also the whole
    of `--check`.
  - `fmt_spec()` — the yt-dlp format selector (the point of the whole tool).
  - `handle_url()` / `handle_playlist()` / `handle_local()` — the three drivers.
  - `tilde()` — display-only helper that collapses `$HOME` back to `~` in every
    printed path, so console output pasted into an issue carries no home
    directory name. Use it for anything printed; never for anything on disk.

## Running it

```bash
pip install -r requirements.txt   # yt-dlp; the script self-installs it too
brew install ffmpeg
python ipod_movie_maker.py                       # interactive: paste URLs/paths
python ipod_movie_maker.py <url|file|folder> …   # flags skip the prompts
python ipod_movie_maker.py --check movie.mp4     # verify only, exits 1 if it won't play
python ipod_movie_maker.py --device touch7 …     # target the A10 instead of the A5
```

Default output is `~/Movies/iPod`. Default `--device` is `touch5`. No config,
no persistent state.

## Conventions & gotchas

- **The three plans are the design.** `copy` (remux only) → `audio` (re-encode
  sound, copy picture) → `encode` (full transcode). Never transcode when a remux
  will do; a link grab normally lands on `copy` because `fmt_spec()`
  asked for `avc1`+`mp4a` at the target height in the first place. If you change
  the format selector, expect the `copy` hit-rate to be what regresses.
- **Download into an empty temp dir, then glob it.** After a merge, yt-dlp's
  `prepare_filename()` still reports the *pre-merge* extension, so globbing a
  dedicated directory is the only reliable way to learn the real output path.
  `merge_output_format` is `mkv` on purpose — it accepts any codec pair
  losslessly, and the mp4 remux happens in our own ffmpeg step anyway.
- **720p is the ceiling, and it is not a taste call.** The panel is 1136 × 640.
  The scale filter uses `min(box, iw/ih)` + `force_original_aspect_ratio=decrease`
  + `force_divisible_by=2` so a small source is never upscaled, an ultra-wide
  source is width-capped rather than letterboxed, and both dimensions stay even
  for `yuv420p`.
- **The default device (A5) has no HEVC decode path at all** — its SoC predates
  HEVC decode hardware (starts at A9) and its iOS (9.3.5) predates HEVC
  software support too (iOS 11+). `video_encoder_args()` refuses `--codec
  hevc` outright on a device whose `DEVICES[...]["vcodecs"]` excludes it —
  loudly, never a silent fallback to H.264 — and the interactive HEVC prompt
  (`ask_codec()`) never even appears for such a device. `--device touch7` is
  the only way to opt into HEVC.
- **8-bit for H.264 on both devices; 10-bit allowed only for HEVC on touch7.**
  The A10 hardware-decodes HEVC Main10 but *not* H.264 High10/4:2:2/4:4:4.
  Getting this backwards produces a file that plays in software, stutters, and
  drains the battery — which looks like "it works" on a desktop test.
- **Main profile costs more bits than High for the same quality** (~3-8%,
  per libx264's own coding-tools gap — no 8x8 transform, no custom
  quantization matrices). `DEVICES["touch5"]["size_mult"]` scales the
  `mb_per_hour` size estimate up accordingly; `touch7` (`encode_profile:
  "high"`) leaves it at `1.0`.
- **Level 3.1's DPB caps reference frames at 5 for a 720p encode**
  (`MaxDpbMbs=18000 / 3600 MBs`). `video_encoder_args()` pins `-refs 3`
  explicitly for touch5 rather than trusting libx264 to auto-clamp it from
  `-level:v 3.1`.
- **`-profile:a` for AAC-LC must be the numeric `1`, not the string
  `"aac_low"`.** The native `aac` encoder accepts either, but `aac_at`
  (AudioToolbox) rejects the named string outright and fails the whole
  encode — verified against a live ffmpeg build, not assumed from docs.
  `audio_encoder_args()` always passes the numeric form so it works on both
  encoders.
- **There is no third-party player on the target device.** The stock Videos app
  is the only one, and the file reaches it via the Apple TV app on the Mac
  (import → Home Videos) then Finder → device → Movies → Sync. Every output has
  to survive Apple's own importer, which is pickier than the device. Do not
  write sideload instructions that assume VLC — that was wrong here.
- **Output is `.m4v` but the mp4 muxer is forced with `-f mp4`.** ffmpeg maps
  the `.m4v` extension to its `ipod` muxer, and `ipod-drop` already found that
  muxer writes tags iOS 9.3.5 rejects. The extension is only there because the
  TV app's importer prefers it; the bytes must stay plain mp4. Check with
  `head -c 12 out.m4v | xxd` — `ftypisom` is right, `ftypM4V ` means the guard
  was lost. `--ext mp4` switches the name back without changing the muxer.
- **`-map_metadata -1`, then one clean `-metadata title=`.** Same lesson: don't
  copy arbitrary source tags into a file iOS 9.3.5 has to parse. A file the TV
  app imports but the iPod refuses looks like success on the Mac, which is the
  worst way for this to fail.
- **Subtitles go in as `mov_text` (tx3g)** with an ISO 639-2 language tag
  (`iso639_2()` — a two-letter code can leave the track unlabelled and hidden).
  Whether the stock iOS 9 Videos app actually renders tx3g is **unconfirmed on a
  real device** — it is what Apple's own tooling writes for iOS, but that was
  never verified against hardware. If it turns out not to work the only
  fallback is burn-in, which needs an ffmpeg built with `libass`. Bitmap subs
  (PGS/VobSub) cannot convert and are dropped with a warning.
- **Not every ffmpeg build is the same.** Some builds ship without `libass` (so
  no `subtitles` filter, so `--burn-subs` cannot work) or without `libzimg` (so
  no `zscale`, so HDR cannot be tone-mapped). `ffmpeg_has()` probes for both at
  runtime and the tool degrades with a printed reason instead of failing or,
  worse, silently shipping a washed-out HDR transfer. The same probe covers the
  optional macOS encoders (`aac_at`, `h264_videotoolbox`, `hevc_videotoolbox`) —
  never assume any of them exists, check. A reader can check their own build
  with `ffmpeg -filters | grep -E ' (subtitles|zscale) '`.
- **Verify the artifact, never the intent.** `convert()` always ends by ffprobing
  what it just wrote. `has_faststart()` walks the MP4 box list by hand rather
  than trusting that `-movflags +faststart` was honoured. A green ffmpeg exit
  code is not evidence the file plays on the device.
- **One dead item must never abort a batch** (same rule as `anydl`). Every queue
  item is wrapped; `Ctrl-C` skips just that item. A playlist writes
  `failed.txt` — every comment line starts with `#`, so
  `grep -v '^#' failed.txt` is a bare URL list to paste back in.
- **Remux failures fall back to a full transcode.** An exotic source (broken
  index, odd stream layout) can fail `-c copy` while transcoding fine, so a
  failed `copy`/`audio` run retries once as `encode` before giving up.
- **Browser cookies are a retry, not a default** — only on a sign-in / bot-check,
  asking once which browser holds the login and caching that for the run. Same
  prompt as `anydl` / `streamlist` / `ipod-drop`.

## Testing without a device

`ffmpeg -f lavfi -i testsrc2=…` plus `-c:v libx265 -pix_fmt yuv420p10le -c:a ac3
-ac 6` builds a source that fails every touch5 row at once — the fastest way to
exercise `encode`. A `libx264 -profile:v main -level 3.1` + `aac -ac 2 -ar 48000`
source exercises `copy`. Neither names anything, so both are safe to describe in
public docs. `--check` on the output is the assertion; `head -c 12 out.m4v | xxd`
is the muxer guard.
