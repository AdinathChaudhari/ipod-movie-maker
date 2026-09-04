<!--
GitHub metadata — authored here, applied by hand in repo Settings (NOT rendered on the page):
About:  Download, remux, transcode & verify video for an iPod touch (5th gen) — a single-file yt-dlp + FFmpeg CLI that picks the cheapest of three plans and ffprobes what it wrote.
Topics (ranked): video-converter, ffmpeg, yt-dlp, ipod, h264, video-transcoding, ipod-touch, ios, python, cli, macos, ffprobe, hardware-decoding, media-conversion, apple-a5, subtitles, m4v
Social preview: docs/media/hero-glass.jpg (1280x640) — derived from docs/media/hero.png by
        `python Tools/image_compress.py docs/media/hero.png 1MB --glass`, uploaded by hand
        in Settings > General > Social preview. The card is git-ignored; hero.png is the source.
-->

# ipod-movie-maker — Video for an iPod touch

**Paste a link. It plays on the iPod.**  
*A single-file CLI that pulls the smallest stream that fits, picks the cheapest of three conversion plans, and ffprobes the result against the device's real decoder limits before calling it done.*

![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)
![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey)
![Requires: FFmpeg](https://img.shields.io/badge/requires-FFmpeg-007808)

<p align="center">
  <img src="docs/media/hero.png" alt="ipod-movie-maker — a film ribbon narrowing into a small glowing pocket screen" width="820">
</p>

A converter that aims at exactly one thing: a 1136 × 640 panel driven by an Apple A5,
frozen at iOS 9.3.5. Everything that device can decode in hardware — codec, profile,
level, pixel format, frame rate, channel count, sample rate — lives in a single `DEVICES`
table, and every decision the tool makes is read out of it. The newer iPod touch 7 (A10
Fusion) is one more entry in that table, reachable with `--device touch7`.

## The map

| What you give it | Plan it picks | What actually happens |
|---|---|---|
| **A video link** | [`copy`](ipod_movie_maker.py) | The format selector already asked for H.264 + AAC at the target height — lossless remux, no re-encode, seconds |
| **An H.264 file with AC3, DTS or Opus sound** | [`audio`](ipod_movie_maker.py) | Picture stream copied untouched, sound re-encoded to 48 kHz stereo AAC |
| **A 4K, HEVC, 10-bit or 60 fps file** | [`encode`](ipod_movie_maker.py) | Full transcode into the panel's envelope: H.264 Main level 3.1, 8-bit, 30 fps, ≤ 720p |
| **A folder or a playlist link** | [one per item](ipod_movie_maker.py) | Each item planned on its own; a dead item lands in `failed.txt` and the batch keeps going |
| **A file you already have** | [`--check`](ipod_movie_maker.py) | ffprobes it against the device envelope and exits non-zero if it will not play |

## Quick start

```bash
brew install ffmpeg                  # required: ffmpeg + ffprobe
pip install -r requirements.txt      # yt-dlp (the script also self-installs it)
python ipod_movie_maker.py           # interactive
```

1. **Paste** links or drop in file and folder paths, one per line, then type `done`.
2. **Pick a quality profile**, or press Enter for `standard` — 720p, roughly 700 MB an hour.
3. **Wait** for the check table. Every row has to pass before the tool claims a file is ready; <kbd>Ctrl-C</kbd> skips just the current item, never the queue.
4. **Sync** the result through the Apple TV app, as described under [Getting it onto the device](#getting-it-onto-the-device).

Files land in `~/Movies/iPod`. That's it — everything below is detail.

---

## A real run

### A source that fails everything: 1080p, 60 fps, HEVC 10-bit, 5.1 AC3

```
$ python ipod_movie_maker.py ~/Movies/sample-clip.mkv

iPod touch (5th gen)  ·  720p standard  ·  H.264  ·  -> ~/Movies/iPod

  source   1920x1080 hevc / ac3   3m 02s   134 MB
  plan     encode - full transcode
             · video is hevc, not H.264
             · 1920x1080 is bigger than the 720p standard box (1280x720)
             · 60 fps is above the decoder limit
             · audio is ac3, not AAC
             · 6-channel audio needs a stereo downmix
  target   720p standard  H.264   ~38 MB

  ✓ ~/Movies/iPod/sample-clip.m4v   49 MB
  Checks:
    ✓ container       .m4v
    ✓ video codec     h264
    ✓ H.264 profile   Main
    ✓ H.264 level     3.1
    ✓ pixel format    yuv420p
    ✓ resolution      1280x720
    ✓ frame rate      30 fps
    ✓ dynamic range   SDR
    ✓ audio codec     aac
    ✓ audio channels  2
    ✓ sample rate     48000 Hz
    ✓ faststart       moov first
```

Five reasons printed, five reasons acted on, twelve rows of proof that the file it wrote
is the file it promised.

### A source that already fits: nothing to encode

This is what a link grab normally looks like, because the format selector asked the site
for H.264 and AAC at the target height in the first place:

```
  source   640x360 h264 / aac   11m 34s   72 MB
  plan     copy - remux only (lossless, no re-encode)

  ✓ ~/Movies/iPod/sample-clip.m4v   72 MB
  Checks:
    ✓ container       .m4v
    ✓ video codec     h264
    ...
    ✓ faststart       moov first
```

### `--check` — will this file play at all?

No encode, no output, just the verdict, and an exit code a script can read:

```
$ python ipod_movie_maker.py --check ~/Movies/sample-clip.mkv

sample-clip.mkv   134 MB
    ✗ container       .mkv
    ✗ video codec     hevc
    ✗ pixel format    yuv420p10le
    ✗ frame rate      60 fps
    ✓ dynamic range   SDR
    ✗ audio codec     ac3
    ✗ audio channels  6
    ✓ sample rate     44100 Hz
    ...

  ✗ Will not play (or not in hardware) - run it through ipod-movie-maker.

$ echo $?
1
```

---

## How it decides

**One: ask for the right bytes.** The yt-dlp format selector requests the *smallest*
stream that still meets the target height, preferring H.264 where the site offers one. A
4K master is never dragged down the wire just to be scaled away, and the download usually
arrives already inside the envelope.

**Two: pick the cheapest plan that works**, and say which and why:

| Plan | When | Cost |
|---|---|---|
| `copy` | the source already fits the envelope | remux only — lossless, seconds |
| `audio` | the picture fits, the sound does not (Opus, AC3, DTS, 5.1) | re-encode audio, copy video |
| `encode` | anything else | full transcode |

A failed `copy` or `audio` retries once as `encode` — an exotic source with a broken index
can defeat `-c copy` and still transcode fine.

**Three: verify the artifact, not the intent.** Every output is ffprobed against the
selected device's real limits before success is claimed: codec, profile, level, pixel
format, resolution, frame rate, dynamic range, audio codec, channels, sample rate, and a
hand-rolled MP4 box walk confirming the `moov` atom really does precede `mdat`. A green
ffmpeg exit code is not evidence that a file plays. `--check` runs that pass alone, on any
file, and exits non-zero when it will not.

---

## Devices & the quality ladder

Both generations share the same 1136 × 640 panel, so **nothing above 720p is visible on
either** — 1080p costs two to three times the storage for detail the screen cannot resolve.

| Profile | Box | ~Size/hr | For |
|---|---|---|---|
| `sharp` | 1280×720 | ~850 MB | text, animation, subtitles, fine detail |
| `standard` **(default)** | 1280×720 | ~700 MB | film and TV — the sweet spot |
| `compact` | 960×540 | ~350 MB | talking heads, lectures |
| `tiny` | 854×480 | ~230 MB | squeezing a 32 GB iPod |

What differs between the two targets is only the codec envelope:

| | `touch5` **(default)** | `touch7` |
|---|---|---|
| Chip / OS | Apple A5, iOS 9.3.5 | A10 Fusion, up to iOS 15 |
| H.264 | Main, level 3.1, 8-bit | High, level 4.2, 8-bit |
| HEVC | none at all | Main and Main10 |
| Frame rate | 30 fps | 60 fps |

The A5 has no HEVC decode path in either hardware or software — its silicon predates
hardware HEVC and its iOS predates the software decoder. `--codec hevc` is therefore
refused outright on `touch5`, with a printed reason, never a silent fallback to H.264.
On `touch7` it produces files around 40% smaller and still decodes in hardware, it just
encodes slower. `--fast` swaps in the macOS VideoToolbox encoder on either target:
roughly ten times quicker, slightly worse per bit.

---

## Getting it onto the device

> **A converted file is not a synced file.** There is no third-party player on the target
> device — the stock **Videos** app is the only one, and it only ever sees what Apple's own
> importer accepted. Drag the `.m4v` into the **Apple TV** app on the Mac (it lands under
> *Home Videos*, not Movies), then plug the iPod in → **Finder** → the device → **Movies** →
> tick it → **Sync**. A file the Mac imports happily but the iPod then refuses is the worst
> way for this to fail, because it looks like success — which is exactly why every output is
> ffprobed before the tool claims one.

Three choices exist purely to keep that path working:

- **Output is `.m4v`, not `.mp4`.** Same bytes, but the Apple TV app's importer is markedly
  happier with Apple's own extension. `--ext mp4` if you would rather.
- **The mp4 muxer is forced with `-f mp4`.** ffmpeg maps the `.m4v` extension to its *ipod*
  muxer, which writes tags iOS 9.3.5 rejects. The name is Apple-flavoured; the bytes stay
  plain mp4. Verify with `head -c 12 out.m4v | xxd` — `ftypisom` is right, `ftypM4V` means
  the guard was lost.
- **Source metadata is dropped and one clean `title` written.** Same reasoning: do not hand
  an iOS 9 parser arbitrary tags from a file it never asked for.

### Movies or TV shows

Apple software decides what a file *is* from one integer — the `stik` atom, buried at
`moov → udta → meta → ilst`. **9 is a movie, 10 is a TV show, and absent is a home video.**
There is no positive code for "home video"; it is the fallback bucket, which is exactly what
this tool produces by default.

`--tv-show` writes `stik=10` plus the three atoms that make episodes group and sort, in the
same single ffmpeg pass — no second tool, and the faststart guarantee survives untouched:

```bash
python ipod_movie_maker.py ep03.mkv --tv-show "Series Name" --season 1 --episode 3
```

| Atom | Written from | Does |
|---|---|---|
| `stik` | `--tv-show` presence | The classification itself — `10` means TV show |
| `tvsh` | `--tv-show` | Series name; episodes group by exact string match |
| `tvsn` | `--season` | Groups episodes into a season |
| `tves` | `--episode` | Orders episodes *within* the season |

Season and episode are not cosmetic. Without them the file still lands under TV Shows but
sorts by filename, which discards the only reason to tag it.

**It changes which pane you sync from.** A TV-show-tagged file leaves the Movies list
entirely: import puts it under *TV Shows*, and Finder syncs it from its own **TV Shows**
pane. Turn **"Automatically include" off** there before syncing — a capped rule like
*10 newest unwatched* can drop episodes with no error. The tool prints this reminder after
any `--tv-show` run, because looking under Movies and finding nothing reads exactly like a
failed encode.

Worth it for a multi-episode series on a device whose stock player has no folders. Not worth
it for a single film — leave those as home videos.

**Subtitles, honestly.** They are muxed as `mov_text` (tx3g), Apple's own in-`.mp4`
subtitle format, tagged with an ISO 639-2 language code so the player lists them. Whether
the stock iOS 9 Videos app actually *renders* them is **unverified on a real device** —
tx3g is what Apple's own tooling writes for iOS, so it should, but nothing short of your
own hardware settles it. If they do not appear, the only fallback is burning them into the
picture, which needs an ffmpeg built with `libass`.

---

## Flags & requirements

```bash
brew install ffmpeg                  # ffmpeg + ffprobe, both required
pip install -r requirements.txt      # yt-dlp
```

| Flag | Does |
|---|---|
| `--device` | `touch5` (default, A5) or `touch7` (A10) — decides every codec, profile and level limit |
| `-p, --profile` | `sharp` / `standard` / `compact` / `tiny` |
| `-c, --codec` | `h264` (default) or `hevc` (`touch7` only — refused on `touch5`) |
| `-o, --out` | output folder (default `~/Movies/iPod`) |
| `--tv-show SERIES` | File it as a TV show episode instead of a home video — changes the sync pane |
| `--season N` / `--episode N` | Season and episode numbers for `--tv-show` (both default to `1`) |
| `--ext` | `m4v` (default) or `mp4` — the name only; the muxer stays mp4 either way |
| `--fast` | macOS hardware encoder — around ten times faster |
| `--force-encode` | transcode even when the source already fits |
| `--burn-subs` | paint subtitles into the picture (needs `libass`) |
| `--no-subs` / `--subs-lang` / `--subs-auto` | subtitle handling |
| `--check FILE` | verify a file plays, then exit |
| `--dry-run` | print the ffmpeg command instead of running it |

**A note on ffmpeg builds.** Not every build carries every filter. Without `libass` there is
no `subtitles` filter, so `--burn-subs` falls back to soft subtitles — fine, iOS takes those
anyway. Without `libzimg` there is no `zscale`, so an HDR source cannot be tone-mapped and
converts washed out and grey. The tool probes for both at runtime and prints what it is
degrading to, rather than failing or silently shipping a bad file. Check your own build with:

```bash
ffmpeg -filters | grep -E ' (subtitles|zscale) '
```

Also handled without ceremony: whole folders, playlists into their own named folder with a
`failed.txt` manifest (`grep -v '^#' failed.txt` gives a bare link list to retry), ultra-wide
and 60 fps and 5.1 and 10-bit sources capped, downmixed and normalised without ever being
upscaled, and a single cookie retry from a browser you pick when a site puts up a sign-in wall.

---

## License & acknowledgements

MIT — see [LICENSE](LICENSE).

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — the download side and the format selector
- [FFmpeg](https://ffmpeg.org) — every remux, transcode and probe
- [anydl](https://github.com/AdinathChaudhari/anydl) — universal downloader, any site, any quality
- [streamlist](https://github.com/AdinathChaudhari/streamlist) — playlists into tagged M4A with cover art
- `ipod-drop` *(archived)* — the audio-only ancestor of this tool, and where the ipod-muxer trap was first found
