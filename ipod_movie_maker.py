#!/usr/bin/env python3
"""ipod-movie-maker — put video on an iPod touch (5th generation), correctly.

Sibling of anydl (universal downloader) and ipod-drop (audio -> iTunes M4A).
Where anydl downloads *anything* at *any* quality, ipod-movie-maker has
exactly one default target and optimises hard for it:

    iPod touch 5  ·  1136 x 640 panel (4", 326 ppi)  ·  A5  ·  iOS 9.3.5

Give it a URL or a local file and it produces an .m4v (plain mp4 bytes) the
device decodes in *hardware*, wasting no bytes on pixels the panel cannot
show. A newer iPod touch 7 (A10 Fusion) is supported as an opt-in target via
--device touch7 - see the DEVICES table, which is the single place either
device's decoder limits live.

Three ways it gets there, cheapest first:

  1. copy    - the source already fits the envelope -> remux only.
               Lossless, seconds, zero quality loss.
  2. audio   - the picture fits but the sound doesn't (Opus/AC3/DTS/...) ->
               copy the video stream, re-encode only the audio.
  3. encode  - full transcode to the chosen profile.

On the download side it asks yt-dlp for the *smallest* stream that still meets
the target height, preferring H.264 where the site offers it - so a 4K source
is never dragged down the wire just to be thrown away, and path 1 hits far
more often than you would expect.

Usage:
    python ipod_movie_maker.py                        # interactive
    python ipod_movie_maker.py <url|file> [...]       # straight to work
    python ipod_movie_maker.py --check movie.mp4      # will this play? (no encode)

Every constant below is derived from a real device's decoder limits and the
1136x640 panel, not from taste. See PROFILES and DEVICES.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile


# ── The devices ───────────────────────────────────────────────────────────────
SCREEN_W, SCREEN_H = 1136, 640          # 4" Retina panel, 326 ppi - identical
                                         # on both generations this tool targets
DEFAULT_OUT = os.path.expanduser("~/Movies/iPod")

# What each device decodes in hardware from a sideloaded .mp4. Outside a given
# device's envelope a file either refuses to play or falls back to a software
# decoder that stutters and drains the battery. This table is the single place
# device facts live - every accept/reject decision (video_fits/audio_fits/
# verify) and every encoder-arg builder reads from DEVICES[opts["device_id"]],
# never a hardcoded limit of its own. That is what stops the tool silently
# offering a codec the selected device cannot decode.
DEVICES = {
    # iPod touch (5th generation) - Apple A5, frozen at iOS 9.3.5 (this model
    # never got an iOS 10+ update). Verbatim from Apple's own spec page
    # (support.apple.com/en-us/112021): "H.264 video up to 720p, 30 frames
    # per second, Main Profile level 3.1 with AAC-LC audio up to 160 Kbps,
    # 48kHz, stereo audio in .m4v, .mp4, and .mov file formats". The A5
    # predates HEVC decode hardware entirely (that starts at A9), and iOS
    # 9.3.5 predates HEVC *software* support in iOS too (added in iOS 11) -
    # so there is no fallback path, hardware or software, that plays HEVC.
    "touch5": {
        "label": "iPod touch (5th gen)",
        "containers": {".mp4", ".m4v", ".mov"},
        "vcodecs": {"h264"},
        "h264_profiles": {"baseline", "constrained baseline", "main"},
        "h264_pix_fmts": {"yuv420p", "yuvj420p"},   # 8-bit only, no High10
        "max_level": 31,                    # H.264 Main profile level 3.1
        "encode_profile": "main",           # what video_encoder_args() targets
        "encode_level": "3.1",
        # Level 3.1's DPB (MaxDpbMbs=18000) fits at most 18000/3600 = 5
        # reference frames at 1280x720 (80x45 = 3600 MBs) - the tightest of
        # the four PROFILES boxes. Pin a safe margin under that explicitly
        # rather than trust libx264 to auto-clamp -refs to the requested level.
        "refs": 3,
        "acodecs": {"aac"},
        "max_channels": 2,                  # one speaker, stereo out
        "sample_rates": {32000, 44100, 48000},
        "force_sample_rate": 48000,         # Apple's spec line names 48kHz
                                             # explicitly - don't pass through
                                             # whatever rate the source has
        # Hard decoder ceiling, not a taste call: 1280x720x30fps lands exactly
        # on level 3.1's MaxMBPS (108000 macroblocks/sec = 3600 MBs x 30fps) -
        # there is zero headroom to round up past 30.
        "max_fps": 30.0,
        # Main profile lacks High's 8x8 integer transform and custom
        # quantization matrices, so it needs ~3-8% more bitrate than High for
        # the same CRF/quality. mb_per_hour below is a High-profile ballpark;
        # scale it up for the size estimate on a device encoded at Main.
        "size_mult": 1.06,
    },
    # iPod touch (7th generation) - Apple A10 Fusion, up to iOS 15. Not the
    # device in hand; kept as an explicit opt-in (`--device touch7`) rather
    # than deleted, since the four PROFILES boxes and the scale-filter logic
    # are identical for both - only the codec envelope differs.
    "touch7": {
        "label": "iPod touch (7th gen)",
        "containers": {".mp4", ".m4v", ".mov"},
        "vcodecs": {"h264", "hevc"},
        "h264_profiles": {"baseline", "constrained baseline", "main", "high"},
        "h264_pix_fmts": {"yuv420p", "yuvj420p"},
        # HEVC Main10 *is* hardware-decoded on the A10, so 10-bit is allowed
        # there (H.264 High10/422/444 is still not - 8-bit only, same as above).
        "hevc_pix_fmts": {"yuv420p", "yuvj420p", "yuv420p10le"},
        "max_level": 42,                    # H.264 High profile level 4.2
        "encode_profile": "high",
        "encode_level": "4.0",
        "refs": None,                       # level 4.2's DPB has slack to spare
        "acodecs": {"aac"},
        "max_channels": 2,
        "sample_rates": {32000, 44100, 48000},
        "force_sample_rate": None,           # High profile source rate is fine
        "max_fps": 60.0,
        "size_mult": 1.0,                    # High profile is the ballpark unit
    },
}
DEFAULT_DEVICE = "touch5"

# 30 fps is invisible on a 4" panel regardless of device, and it doubles as
# the touch5 hard decode ceiling (DEVICES["touch5"]["max_fps"]) - so this stays
# the encode target even when --device touch7 would tolerate more.
MAX_FPS = 30

# Quality ladders. crf = libx264 CRF; hevc_crf ~= crf + 5 for equal quality
# (hevc_crf/hevc_br are only reachable via --device touch7 --codec hevc -
# touch5 has no HEVC decode path at all, see DEVICES); br = target bitrate for
# the hardware (--fast) encoders, which ignore CRF. mb_per_hour is a
# live-action ballpark at High profile, used only for the size estimate -
# DEVICES[...]["size_mult"] scales it for Main profile's lower efficiency.
PROFILES = [
    {"key": "sharp", "w": 1280, "h": 720, "crf": 20, "hevc_crf": 25,
     "br": "2200k", "hevc_br": "1300k", "mb_per_hour": 850,
     "label": "720p sharp",
     "note": "text, anime, subtitles, fine detail"},
    {"key": "standard", "w": 1280, "h": 720, "crf": 21, "hevc_crf": 26,
     "br": "1600k", "hevc_br": "950k", "mb_per_hour": 700,
     "label": "720p standard",
     "note": "film & TV - the sweet spot"},
    {"key": "compact", "w": 960, "h": 540, "crf": 22, "hevc_crf": 27,
     "br": "900k", "hevc_br": "550k", "mb_per_hour": 350,
     "label": "540p compact",
     "note": "talking heads, lectures - identical at 4 inches"},
    {"key": "tiny", "w": 854, "h": 480, "crf": 24, "hevc_crf": 29,
     "br": "600k", "hevc_br": "380k", "mb_per_hour": 230,
     "label": "480p tiny",
     "note": "squeeze a 32 GB iPod / mostly-listening viewing"},
]
DEFAULT_PROFILE = "standard"

# Text subtitle codecs we can turn into mov_text (tx3g) - Apple's own subtitle
# format inside .mp4 and the only soft-subtitle format the stock Videos app
# has any chance of rendering - which matters because this device has no
# third-party player installed. Bitmap
# subs cannot become mov_text; they have to be burned into the picture.
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
BITMAP_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
SUB_FILE_EXTS = {".srt", ".vtt", ".ass", ".ssa"}

VIDEO_FILE_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv",
                   ".ts", ".mpg", ".mpeg", ".wmv", ".3gp", ".m2ts", ".ogv"}

TICK, CROSS, WARN = "✓", "✗", "!"


# ── Small helpers ─────────────────────────────────────────────────────────────
def safe_filename(name):
    """Strip characters not allowed in file/folder names."""
    return re.sub(r'[\\/:*?"<>|]', "", name or "").strip() or "video"


def tilde(path):
    """Collapse the home directory back to `~` for display only.

    Every path this tool prints is somewhere under the user's home, so the
    console (and anything pasted from it into a bug report) would otherwise
    carry a home directory name for no benefit."""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def unique_path(path):
    """Return path, or 'name (1).ext' etc. if it already exists."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def human_size(nbytes):
    val = float(nbytes or 0)
    for unit in ("B", "KB", "MB"):
        if val < 1024:
            return f"{val:.0f} {unit}"
        val /= 1024.0
    return f"{val:.2f} GB"


def human_time(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def profile_by_key(key):
    for p in PROFILES:
        if p["key"] == key:
            return p
    raise SystemExit(f"Unknown profile '{key}'. Choose from: "
                     + ", ".join(p["key"] for p in PROFILES))


def is_url(s):
    return s.startswith(("http://", "https://", "www."))


# The .mp4 container stores subtitle languages as ISO 639-2/T three-letter
# codes. A two-letter code written straight through can leave the track
# effectively unlabelled, which is enough for the stock player to hide it.
_ISO639_2 = {"en": "eng", "hi": "hin", "mr": "mar", "es": "spa", "fr": "fra",
             "de": "deu", "it": "ita", "pt": "por", "ru": "rus", "ja": "jpn",
             "ko": "kor", "zh": "zho", "ar": "ara", "ta": "tam", "te": "tel",
             "bn": "ben", "gu": "guj", "kn": "kan", "ml": "mal", "pa": "pan"}


def iso639_2(code):
    """Best-effort two-letter -> three-letter language code."""
    base = (code or "en").split("-")[0].lower()
    return _ISO639_2.get(base, base)


# ── ffmpeg / ffprobe ──────────────────────────────────────────────────────────
def require_ffmpeg():
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        raise SystemExit(f"Missing {' and '.join(missing)}. "
                         "Install with:  brew install ffmpeg")


_CAPS = {}


def ffmpeg_has(kind, name):
    """True if this ffmpeg build exposes an encoder/filter called `name`."""
    key = (kind, name)
    if key in _CAPS:
        return _CAPS[key]
    flag = {"encoder": "-encoders", "filter": "-filters"}[kind]
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", flag],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        out = ""
    _CAPS[key] = re.search(rf"^\s*\S+\s+{re.escape(name)}\s", out, re.M) is not None
    return _CAPS[key]


def probe_media(path):
    """ffprobe -> {'format': {...}, 'streams': [...]} or None if unreadable."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            text=True, stderr=subprocess.DEVNULL)
        return json.loads(out)
    except Exception:
        return None


def streams_of(info, kind):
    """All streams of codec_type `kind`, skipping attached cover art, which
    ffprobe reports as a video stream and which would otherwise be mistaken
    for the picture."""
    out = []
    for s in (info or {}).get("streams", []):
        if s.get("codec_type") != kind:
            continue
        if kind == "video" and (s.get("disposition") or {}).get("attached_pic"):
            continue
        out.append(s)
    return out


def pick_stream(info, kind):
    got = streams_of(info, kind)
    return got[0] if got else None


def parse_fps(stream):
    """Frame rate as a float, from avg/r_frame_rate ('30000/1001')."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        val = (stream or {}).get(key) or ""
        if "/" in val:
            num, den = val.split("/", 1)
            try:
                num, den = float(num), float(den)
            except ValueError:
                continue
            if den and num:
                return num / den
    return 0.0


def duration_of(info):
    try:
        return float((info or {}).get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def is_hdr(vstream):
    """HDR10 / HLG source. Without tone-mapping it plays washed-out and grey."""
    return (vstream or {}).get("color_transfer") in ("smpte2084", "arib-std-b67")


def has_faststart(path):
    """True if the moov atom precedes mdat - i.e. playback starts before the
    file is fully buffered. Walks the top-level MP4 box list directly."""
    try:
        with open(path, "rb") as f:
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    return False
                size = int.from_bytes(hdr[:4], "big")
                typ = hdr[4:8]
                offset = 8
                if size == 1:                       # 64-bit extended size
                    size = int.from_bytes(f.read(8), "big")
                    offset = 16
                elif size == 0:                     # box runs to end of file
                    return typ == b"moov"
                if typ == b"moov":
                    return True
                if typ == b"mdat":
                    return False
                if size < offset:
                    return False
                f.seek(size - offset, os.SEEK_CUR)
    except OSError:
        return False


# ── Does this file already fit the target device? ─────────────────────────────
_CODEC_LABEL = {"h264": "H.264", "hevc": "HEVC"}


def video_fits(v, prof, device):
    """(fits, [reasons it doesn't]) for the video stream against a profile
    and the selected device's DEVICES envelope."""
    if not v:
        return False, ["no video stream"]
    reasons = []
    codec = v.get("codec_name")
    if codec not in device["vcodecs"]:
        allowed = "/".join(_CODEC_LABEL.get(c, c) for c in sorted(device["vcodecs"]))
        reasons.append(f"video is {codec or '?'}, not {allowed}")
    else:
        allowed_fmts = (device.get("hevc_pix_fmts", set()) if codec == "hevc"
                        else device["h264_pix_fmts"])
        if v.get("pix_fmt") not in allowed_fmts:
            reasons.append(f"{v.get('pix_fmt') or '?'} is not a hardware-decoded "
                           "pixel format")
        if codec == "h264":
            prof_name = (v.get("profile") or "").lower()
            if prof_name and prof_name not in device["h264_profiles"]:
                reasons.append(f"H.264 {v.get('profile')} profile is not "
                               "hardware-decoded")
            level = v.get("level") or 0
            if level and level > device["max_level"]:
                reasons.append(f"H.264 level {level / 10:.1f} exceeds "
                               f"{device['max_level'] / 10:.1f}")

    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if h > prof["h"] or w > prof["w"]:
        reasons.append(f"{w}x{h} is bigger than the {prof['label']} box "
                       f"({prof['w']}x{prof['h']})")
    fps = parse_fps(v)
    if fps > device["max_fps"]:
        reasons.append(f"{fps:.0f} fps is above the decoder limit")
    if is_hdr(v):
        reasons.append("HDR source needs tone-mapping to SDR")
    return (not reasons), reasons


def audio_fits(a, device):
    """(fits, [reasons]) for the audio stream against the selected device's
    envelope. No audio at all is fine."""
    if not a:
        return True, []
    reasons = []
    codec = a.get("codec_name")
    if codec not in device["acodecs"]:
        reasons.append(f"audio is {codec or '?'}, not AAC")
    if int(a.get("channels") or 2) > device["max_channels"]:
        reasons.append(f"{a.get('channels')}-channel audio needs a stereo downmix")
    # Sample rate has to be judged here, not only in audio_encoder_args(). A
    # 96 kHz AAC stream is otherwise waved straight through the `copy` path,
    # since that path never re-encodes the audio and so never resamples it.
    sr = int(a.get("sample_rate") or 0)
    if sr and sr not in device["sample_rates"]:
        reasons.append(f"{sr} Hz audio is outside the rates the device accepts")
    return (not reasons), reasons


# ── Subtitles ─────────────────────────────────────────────────────────────────
def find_sidecar_subs(media_path):
    """Sidecar .srt/.vtt/.ass sitting next to a media file (yt-dlp writes these
    with a language suffix: 'Title.en.srt')."""
    base = os.path.splitext(media_path)[0]
    found = []
    for ext in SUB_FILE_EXTS:
        found += glob.glob(glob.escape(base) + "*" + ext)
    return sorted(set(found))


def classify_embedded_subs(info):
    """(text_stream_indices, n_bitmap) among the source's subtitle streams."""
    text_idx, bitmap = [], 0
    for i, s in enumerate(streams_of(info, "subtitle")):
        codec = s.get("codec_name")
        if codec in TEXT_SUB_CODECS:
            text_idx.append(i)
        elif codec in BITMAP_SUB_CODECS:
            bitmap += 1
    return text_idx, bitmap


# ── Planning ──────────────────────────────────────────────────────────────────
def extract_sub_to(src, stream_idx, dst):
    """Pull one embedded text subtitle track out to a standalone .srt."""
    rc = subprocess.call(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-map", f"0:s:{stream_idx}", "-c:s", "srt", dst],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dst if rc == 0 and os.path.exists(dst) and os.path.getsize(dst) else None


def stage_burn_source(src, sidecars, text_idx, workdir):
    """Return an ASCII temp path holding the subtitles to burn, or None.

    Staging into the workdir is deliberate: the `subtitles` filter argument
    would otherwise have to escape colons, quotes and brackets out of the real
    filename, which is a well-known source of silent ffmpeg breakage."""
    if sidecars:
        staged = os.path.join(workdir, "burn" + os.path.splitext(sidecars[0])[1])
        shutil.copyfile(sidecars[0], staged)
        return staged
    if text_idx:
        return extract_sub_to(src, text_idx[0], os.path.join(workdir, "burn.srt"))
    return None


def build_plan(src, prof, opts, workdir):
    """Decide copy / audio / encode for one source file, and why.

    Returns a dict the ffmpeg builder and the console report both read from."""
    info = probe_media(src)
    if not info:
        return None
    device = opts["device"]
    v, a = pick_stream(info, "video"), pick_stream(info, "audio")
    vfit, vwhy = video_fits(v, prof, device)
    afit, awhy = audio_fits(a, device)

    sidecars = [] if opts["no_subs"] else find_sidecar_subs(src)
    text_idx, n_bitmap = ([], 0) if opts["no_subs"] else classify_embedded_subs(info)

    burn_src = None
    if opts["burn_subs"] and (sidecars or text_idx):
        if not ffmpeg_has("filter", "subtitles"):
            # Burning needs libass. Soft mov_text is the only other option on
            # a device with no third-party player - see the subtitle note in
            # README.md, this fallback is not guaranteed to be visible.
            print(f"  {WARN} --burn-subs needs an ffmpeg built with libass and "
                  "this one has none. Using soft mov_text subtitles instead - "
                  "check they actually appear in the Videos app.")
        else:
            burn_src = stage_burn_source(src, sidecars, text_idx, workdir)
            if not burn_src:
                print(f"  {WARN} Could not extract a text subtitle track to "
                      "burn in; using soft subtitles instead.")
    if burn_src:
        # Hardcoding subtitles paints them into the picture - that is a
        # transcode by definition, no matter how well the source fits.
        vfit = False
        vwhy = vwhy + ["subtitles are being burned in"]

    if opts["force_encode"] and vfit:
        vfit = False
        vwhy = vwhy + ["--force-encode was requested"]

    if vfit and afit:
        mode = "copy"
    elif vfit:
        mode = "audio"
    else:
        mode = "encode"

    return {
        "info": info, "v": v, "a": a, "mode": mode,
        "video_why": vwhy, "audio_why": awhy,
        "sidecars": [] if burn_src else sidecars,
        "burn": burn_src,
        "embedded_text_subs": [] if burn_src else text_idx,
        "bitmap_subs": n_bitmap,
        "duration": duration_of(info),
        "src_size": os.path.getsize(src) if os.path.exists(src) else 0,
    }


def estimate_size(plan, prof, opts):
    """Rough output size in bytes. A copy/audio remux keeps the source size."""
    if plan["mode"] != "encode":
        return plan["src_size"]
    hours = (plan["duration"] or 0) / 3600.0
    mb = prof["mb_per_hour"] * hours
    if opts["codec"] == "hevc":
        mb *= 0.58
    else:
        mb *= opts["device"]["size_mult"]   # Main profile costs more than High
    return int(mb * 1024 * 1024)


# ── ffmpeg command construction ───────────────────────────────────────────────
def video_filter_chain(plan, prof, opts):
    """The -vf chain for encode mode: tone-map -> burn subs -> fps -> scale."""
    chain = []
    if is_hdr(plan["v"]):
        if ffmpeg_has("filter", "zscale") and ffmpeg_has("filter", "tonemap"):
            chain += ["zscale=t=linear:npl=100", "format=gbrpf32le",
                      "zscale=p=bt709", "tonemap=tonemap=hable:desat=0",
                      "zscale=t=bt709:m=bt709:r=tv"]
        else:
            print(f"  {WARN} HDR source but this ffmpeg has no zscale/tonemap - "
                  "colours will look washed out. `brew install ffmpeg` for a "
                  "build with libzimg.")
    if plan["burn"]:
        # The file was copied to a plain ASCII temp path precisely so this
        # filter argument needs no escaping gymnastics.
        chain.append(f"subtitles={plan['burn']}")
    # Decimate against the DEVICE's ceiling, not a module constant. touch5 tops
    # out at exactly 30.0, so a 30.2 fps source has to be resampled here: it is
    # already routed to `encode` by video_fits(), and if the filter chain leaves
    # the rate alone the encode fails its own verify() with no remedy left to
    # offer. The 0.01 slack keeps 29.97 (30000/1001) from being touched.
    # min() of the two: MAX_FPS is the taste call (30 is all a 4" panel can
    # show), the device ceiling is the hard limit. Taking the lower keeps
    # touch7 at 30 as intended while still guaranteeing touch5's 30.0 is met.
    max_fps = min(MAX_FPS, opts["device"]["max_fps"])
    fps = parse_fps(plan["v"])
    if fps > max_fps + 0.01:
        chain.append(f"fps={max_fps:g}")
    # min() so a small source is never upscaled; decrease-fit preserves aspect;
    # force_divisible_by=2 keeps both dimensions even for yuv420p.
    chain.append(f"scale='min({prof['w']},iw)':'min({prof['h']},ih)':"
                 "force_original_aspect_ratio=decrease:force_divisible_by=2")
    chain.append("format=yuv420p")
    return ",".join(chain)


def video_encoder_args(prof, opts):
    device = opts["device"]
    if opts["codec"] == "hevc":
        if "hevc" not in device["vcodecs"]:
            # Refuse outright, loudly - never silently drop to H.264. A quiet
            # downgrade here would leave --dry-run output and the actual
            # command disagreeing about what got encoded.
            raise SystemExit(
                f"  {CROSS} --codec hevc was requested but {device['label']} "
                "has no HEVC decode path at all (its SoC predates HEVC "
                "decode hardware, and its iOS predates HEVC software support "
                "too). Drop --codec, or pass --device touch7.")
        if opts["fast"] and ffmpeg_has("encoder", "hevc_videotoolbox"):
            return ["-c:v", "hevc_videotoolbox", "-b:v", prof["hevc_br"],
                    "-tag:v", "hvc1"]
        # hvc1 tag (not hev1) is what Apple's demuxer expects in an .mp4.
        return ["-c:v", "libx265", "-preset", "medium",
                "-crf", str(prof["hevc_crf"]), "-tag:v", "hvc1"]
    if opts["fast"] and ffmpeg_has("encoder", "h264_videotoolbox"):
        return ["-c:v", "h264_videotoolbox", "-b:v", prof["br"],
                "-profile:v", device["encode_profile"],
                "-level:v", device["encode_level"]]
    args = ["-c:v", "libx264", "-preset", "slow", "-crf", str(prof["crf"]),
            "-profile:v", device["encode_profile"],
            "-level:v", device["encode_level"]]
    if device["refs"]:
        args += ["-refs", str(device["refs"])]   # see DEVICES[...]["refs"]
    return args


def audio_encoder_args(plan, opts):
    device = opts["device"]
    # aac_at is Apple's AudioToolbox encoder - better than native aac at 128k,
    # same choice streamlist and ipod-drop make. -profile:a 1 pins AAC-LC
    # explicitly (numeric AV_PROFILE_AAC_LOW, not the string "aac_low" -
    # aac_at's AVOption parser rejects the named string outright and errors
    # out the whole encode; only the numeric value is accepted, and it works
    # on both aac_at and the native aac encoder). Apple's spec for both
    # devices names AAC-LC explicitly, so pin it rather than trust a future
    # encoder's default.
    enc = "aac_at" if ffmpeg_has("encoder", "aac_at") else "aac"
    args = ["-c:a", enc, "-profile:a", "1", "-b:a", "128k", "-ac", "2"]
    sr = int((plan["a"] or {}).get("sample_rate") or 0)
    if device["force_sample_rate"]:
        args += ["-ar", str(device["force_sample_rate"])]
    elif sr and sr not in device["sample_rates"]:
        args += ["-ar", "48000"]        # only resample when we have to
    return args


def build_ffmpeg_cmd(src, dst, plan, prof, opts):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y",
           "-i", src]
    for sub in plan["sidecars"]:
        cmd += ["-i", sub]

    cmd += ["-map", "0:v:0"]
    if plan["a"]:
        cmd += ["-map", "0:a:0"]
    for i in plan["embedded_text_subs"]:
        cmd += ["-map", f"0:s:{i}?"]
    for n, _ in enumerate(plan["sidecars"], start=1):
        cmd += ["-map", f"{n}:s:0?"]

    if plan["mode"] == "copy":
        cmd += ["-c:v", "copy", "-c:a", "copy"]
    elif plan["mode"] == "audio":
        cmd += ["-c:v", "copy"] + audio_encoder_args(plan, opts)
    else:
        vf = video_filter_chain(plan, prof, opts)
        cmd += video_encoder_args(prof, opts) + ["-vf", vf, "-pix_fmt", "yuv420p"]
        cmd += audio_encoder_args(plan, opts) if plan["a"] else []

    if plan["embedded_text_subs"] or plan["sidecars"]:
        cmd += ["-c:s", "mov_text"]

    # Drop the source's metadata instead of copying it. ipod-drop learned the
    # hard way that ffmpeg-written tags can be rejected outright by iOS 9.3.5,
    # and a file the TV app imports happily but the iPod then refuses is the
    # worst failure mode this tool has - it looks like success on the Mac.
    # Carry across one clean title and nothing else.
    cmd += ["-map_metadata", "-1",
            "-metadata", "title=" + os.path.splitext(os.path.basename(dst))[0]]
    if plan["embedded_text_subs"] or plan["sidecars"]:
        # An untagged subtitle track may never appear in the stock player's
        # subtitle menu, which on this device is the only player there is.
        cmd += ["-metadata:s:s:0", "language=" + iso639_2(opts["subs_lang"])]
    # Force the mp4 muxer even when the output is named .m4v. ffmpeg maps that
    # extension to its `ipod` muxer, which is precisely the muxer ipod-drop
    # found writes tags iOS 9.3.5 rejects. The .m4v name exists only to keep
    # the Apple TV app's import path happy; the bytes stay plain mp4.
    cmd += ["-f", "mp4", "-max_muxing_queue_size", "1024",
            "-movflags", "+faststart", dst]
    return cmd


# ── Verification ──────────────────────────────────────────────────────────────
def verify(path, device, prof=None):
    """Check a finished file against the selected device's DEVICES envelope.
    Prints a table and returns True only if everything the device needs is
    satisfied - a High-profile, level>3.1, HEVC, 10-bit, or >30fps file all
    fail this against `device=DEVICES["touch5"]`, which is the whole point.

    `prof` additionally checks the file is not larger than that profile's box -
    an oversized file still plays, it just wastes storage, so that row is a
    warning rather than a failure."""
    info = probe_media(path)
    if not info:
        print(f"  {CROSS} ffprobe could not read {path}")
        return False
    v, a = pick_stream(info, "video"), pick_stream(info, "audio")
    rows = []   # (status, label, detail)   status: True / False / "warn"

    ext = os.path.splitext(path)[1].lower()
    rows.append((ext in device["containers"], "container", ext or "?"))

    codec = (v or {}).get("codec_name")
    rows.append((codec in device["vcodecs"], "video codec", codec or "none"))

    if codec == "h264":
        pname = (v.get("profile") or "").lower()
        rows.append((pname in device["h264_profiles"],
                     "H.264 profile", v.get("profile") or "?"))
        level = v.get("level") or 0
        rows.append((bool(level) and level <= device["max_level"],
                     "H.264 level", f"{level / 10:.1f}" if level else "?"))
    allowed_fmts = (device.get("hevc_pix_fmts", set()) if codec == "hevc"
                    else device["h264_pix_fmts"])
    rows.append(((v or {}).get("pix_fmt") in allowed_fmts,
                 "pixel format", (v or {}).get("pix_fmt") or "?"))

    w, h = int((v or {}).get("width") or 0), int((v or {}).get("height") or 0)
    if prof:
        rows.append((True if (h <= prof["h"] and w <= prof["w"]) else "warn",
                     "resolution",
                     f"{w}x{h}" + ("" if h <= prof["h"] else
                                   f" (over the {prof['label']} box)")))
    else:
        rows.append((True if h <= 720 else "warn", "resolution",
                     f"{w}x{h}" + ("" if h <= 720 else
                                   " - larger than the panel can show")))

    fps = parse_fps(v)
    rows.append((fps <= device["max_fps"], "frame rate", f"{fps:.3g} fps"))
    rows.append((not is_hdr(v), "dynamic range",
                 "HDR (will look grey)" if is_hdr(v) else "SDR"))

    if a:
        rows.append((a.get("codec_name") in device["acodecs"],
                     "audio codec", a.get("codec_name") or "?"))
        ch = int(a.get("channels") or 0)
        rows.append((ch <= device["max_channels"], "audio channels",
                     str(ch)))
        sr = int(a.get("sample_rate") or 0)
        rows.append((bool(sr) and sr in device["sample_rates"], "sample rate",
                     f"{sr} Hz" if sr else "?"))
    else:
        rows.append(("warn", "audio", "none"))

    rows.append((True if has_faststart(path) else "warn", "faststart",
                 "moov first" if has_faststart(path) else "moov at end"))

    n_subs = len(streams_of(info, "subtitle"))
    if n_subs:
        codecs = {s.get("codec_name") for s in streams_of(info, "subtitle")}
        rows.append((codecs <= {"mov_text"}, "subtitles",
                     f"{n_subs} x {', '.join(sorted(c or '?' for c in codecs))}"))

    width = max(len(label) for _, label, _ in rows)
    for status, label, detail in rows:
        mark = TICK if status is True else (WARN if status == "warn" else CROSS)
        print(f"    {mark} {label.ljust(width)}  {detail}")
    return all(s is not False for s, _, _ in rows)


# ── Conversion ────────────────────────────────────────────────────────────────
MODE_BLURB = {
    "copy": "remux only (lossless, no re-encode)",
    "audio": "audio re-encode, video copied untouched",
    "encode": "full transcode",
}


def convert(src, out_dir, title, prof, opts, workdir):
    """Fit one media file for the iPod. Returns the output path, or None."""
    plan = build_plan(src, prof, opts, workdir)
    if not plan:
        print(f"  {CROSS} Not a media file ffprobe can read: {src}")
        return None
    if not plan["v"]:
        print(f"  {CROSS} No video stream in {os.path.basename(src)} - "
              "use ipod-drop / streamlist for audio.")
        return None

    os.makedirs(out_dir, exist_ok=True)
    dst = unique_path(os.path.join(out_dir,
                                   safe_filename(title) + "." + opts["ext"]))

    v = plan["v"]
    print(f"\n  source   {v.get('width')}x{v.get('height')} "
          f"{v.get('codec_name') or '?'} / "
          f"{(plan['a'] or {}).get('codec_name') or 'no audio'}"
          f"   {human_time(plan['duration'])}   {human_size(plan['src_size'])}")
    print(f"  plan     {plan['mode']} - {MODE_BLURB[plan['mode']]}")
    for why in plan["video_why"] + plan["audio_why"]:
        print(f"             · {why}")
    if plan["mode"] == "encode":
        print(f"  target   {prof['label']}  "
              f"{'HEVC' if opts['codec'] == 'hevc' else 'H.264'}"
              f"{'  (hardware encoder)' if opts['fast'] else ''}"
              f"   ~{human_size(estimate_size(plan, prof, opts))}")
    if plan["bitmap_subs"]:
        hint = ("Supply an external .srt and re-run with --burn-subs."
                if ffmpeg_has("filter", "subtitles")
                else "Supply an external .srt (burning them in would need an "
                     "ffmpeg with libass).")
        print(f"  {WARN} {plan['bitmap_subs']} bitmap subtitle track(s) dropped - "
              f"PGS/VobSub can't become mov_text. {hint}")
    if plan["embedded_text_subs"] or plan["sidecars"]:
        print(f"  subs     {len(plan['embedded_text_subs']) + len(plan['sidecars'])}"
              " track(s) as mov_text (tagged for the Videos app)")

    cmd = build_ffmpeg_cmd(src, dst, plan, prof, opts)
    if opts["dry_run"]:
        print("\n  " + " ".join(cmd))
        return None
    print()
    rc = subprocess.call(cmd)
    if rc != 0 or not os.path.exists(dst):
        # A copy/audio remux can fail on an exotic source (odd stream layout,
        # broken index). A full transcode almost never does - so fall back.
        if plan["mode"] != "encode":
            print(f"  {WARN} Remux failed; falling back to a full transcode.")
            plan["mode"] = "encode"
            plan["video_why"].append("remux failed on this source")
            cmd = build_ffmpeg_cmd(src, dst, plan, prof, opts)
            rc = subprocess.call(cmd)
        if rc != 0 or not os.path.exists(dst):
            print(f"  {CROSS} ffmpeg failed (exit {rc}).")
            if os.path.exists(dst):
                os.remove(dst)
            return None

    print(f"\n  {TICK} {tilde(dst)}   {human_size(os.path.getsize(dst))}")
    print("  Checks:")
    if not verify(dst, opts["device"], prof):
        print(f"  {CROSS} This file does NOT meet the {opts['device']['label']} "
              "envelope - re-run with --force-encode.")
    return dst


# ── yt-dlp ────────────────────────────────────────────────────────────────────
def ensure_yt_dlp():
    try:
        import yt_dlp
    except ImportError:
        print("Installing yt-dlp...")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "yt-dlp>=2026.3.17"])
        import yt_dlp
    return yt_dlp


# remote_components ejs:github is needed for current YouTube extraction; every
# other extractor ignores it, so it is safe to pass globally. Same as anydl.
_BASE_OPTS = {"quiet": True, "no_warnings": True,
              "remote_components": ["ejs:github"]}

BROWSERS = ["safari", "chrome", "firefox", "edge", "brave", "opera"]
_COOKIE_BROWSER = False   # False = not asked, None = declined, str = chosen


def _needs_cookies(err):
    s = str(err).lower()
    return ("sign in to confirm" in s or "not a bot" in s
            or "confirm you" in s or "sign in to view" in s)


def choose_cookie_browser():
    """Ask once which browser holds the login; reuse for the rest of the run.
    We ask rather than auto-detect by disk path because only you know which
    browser you are actually signed in on (same prompt as anydl/streamlist)."""
    global _COOKIE_BROWSER
    if _COOKIE_BROWSER is not False:
        return _COOKIE_BROWSER
    print("\n  This site wants a sign-in cookie. Which browser are you logged "
          "into it on?")
    for i, b in enumerate(BROWSERS, 1):
        print(f"    {i}. {b.capitalize()}")
    print("    0. None / skip")
    while True:
        ans = input("  Browser [0-6]: ").strip()
        if ans in ("0", ""):
            _COOKIE_BROWSER = None
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(BROWSERS):
            _COOKIE_BROWSER = BROWSERS[int(ans) - 1]
            return _COOKIE_BROWSER
        print("  Enter a number 0-6.")


def cookie_opts():
    b = choose_cookie_browser()
    return {"cookiesfrombrowser": (b, None, None, None)} if b else {}


def _extract(fn, seed=None):
    """Run fn(extra_opts), retrying once with browser cookies on a sign-in /
    bot-check. Returns (result, cookie_extra_that_worked) or None."""
    from yt_dlp.utils import DownloadError
    base = dict(seed or {})
    try:
        return fn(base), base
    except DownloadError as e:
        if not _needs_cookies(e) or base:
            print(f"  yt-dlp couldn't extract: {str(e)[:200]}")
            return None
        ck = cookie_opts()
        if not ck:
            print("  Skipped cookies - this URL needs a sign-in.")
            return None
        print(f"  Retrying with {_COOKIE_BROWSER} cookies "
              "(your keychain may prompt)...")
        try:
            return fn(ck), ck
        except DownloadError as e2:
            print(f"  yt-dlp still couldn't extract: {str(e2)[:200]}")
            return None


def fmt_spec(prof):
    """Ask for the SMALLEST stream that still meets the target height, in H.264
    where it exists.

    This is the whole point of a device-specific downloader: a 4K master is
    never pulled down the wire only to be scaled away, and when the site does
    serve avc1+m4a at this height the result needs no transcode at all."""
    h = prof["h"]
    return (
        f"bestvideo[vcodec^=avc1][height<={h}][fps<=31]+bestaudio[acodec^=mp4a]"
        f"/bestvideo[vcodec^=avc1][height<={h}]+bestaudio[acodec^=mp4a]"
        f"/bestvideo[vcodec^=avc1][height<={h}]+bestaudio"
        f"/bestvideo[height<={h}]+bestaudio"
        f"/best[height<={h}]"
        f"/best"
    )


def download_to(url, workdir, prof, opts, yt_dlp, cookie_extra=None):
    """Download one URL into an empty workdir. Returns (media_path, title).

    Downloading into a dedicated empty directory and then globbing it is the
    one reliable way to learn the final filename after a merge - yt-dlp's
    prepare_filename() reports the pre-merge extension."""
    ydl_opts = dict(_BASE_OPTS)
    ydl_opts.update({
        "format": fmt_spec(prof),
        "outtmpl": os.path.join(workdir, "%(title).150s.%(ext)s"),
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "merge_output_format": "mkv",   # lossless container; we remux after
    })
    if not opts["no_subs"]:
        ydl_opts.update({
            "writesubtitles": True,
            "writeautomaticsub": opts["subs_auto"],
            "subtitleslangs": [opts["subs_lang"], opts["subs_lang"] + ".*"],
            "subtitlesformat": "srt/vtt/best",
        })
    if cookie_extra:
        ydl_opts.update(cookie_extra)

    def run(extra):
        o = dict(ydl_opts)
        o.update(extra or {})
        with yt_dlp.YoutubeDL(o) as ydl:
            return ydl.extract_info(url, download=True)

    got = _extract(run, seed=cookie_extra)
    if not got:
        return None, None
    info = got[0] or {}

    media = [p for p in glob.glob(os.path.join(workdir, "*"))
             if os.path.splitext(p)[1].lower() in VIDEO_FILE_EXTS]
    if not media:
        print(f"  {CROSS} Nothing downloadable found at that URL.")
        return None, None
    media.sort(key=os.path.getsize, reverse=True)
    return media[0], (info.get("title") or os.path.splitext(
        os.path.basename(media[0]))[0])


# ── Failure manifest (house convention: one dead item never aborts a batch) ───
FAILED_MANIFEST = "failed.txt"


def write_failed_manifest(folder, playlist_title, failed, total):
    """Write <folder>/failed.txt so a 200-item run leaves a paper trail the
    console scrollback cannot. Every comment starts with '#', so
    `grep -v '^#' failed.txt` is a bare URL list you can paste straight back
    into ipod-movie-maker to retry. Never raises."""
    if not failed:
        return
    try:
        path = os.path.join(folder, FAILED_MANIFEST)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {playlist_title}\n")
            f.write(f"# {len(failed)} of {total} item(s) failed\n")
            f.write("# grep -v '^#' failed.txt  ->  a plain URL list to retry\n\n")
            for idx, title, reason, url in failed:
                f.write(f"# [{idx}] {title} - {reason}\n")
                if url:          # no blank lines in the grep-able URL list
                    f.write(url + "\n")
        print(f"\n  Wrote {path}")
    except Exception:
        pass


# ── Per-item drivers ──────────────────────────────────────────────────────────
def handle_local(path, prof, opts):
    if os.path.isdir(path):
        files = sorted(p for p in glob.glob(os.path.join(path, "*"))
                       if os.path.splitext(p)[1].lower() in VIDEO_FILE_EXTS)
        if not files:
            print(f"  {CROSS} No video files in {path}")
            return False
        print(f"  {len(files)} video file(s) in this folder.")
        ok = 0
        for i, f in enumerate(files, 1):
            print(f"\n  [{i}/{len(files)}] {os.path.basename(f)}")
            with tempfile.TemporaryDirectory(prefix="ipodmoviemaker-") as wd:
                if convert(f, opts["out"], os.path.splitext(
                        os.path.basename(f))[0], prof, opts, wd):
                    ok += 1
        print(f"\n  {ok}/{len(files)} converted.")
        return ok > 0

    title = os.path.splitext(os.path.basename(path))[0]
    with tempfile.TemporaryDirectory(prefix="ipodmoviemaker-") as wd:
        return bool(convert(path, opts["out"], title, prof, opts, wd))


def handle_playlist(url, info, prof, opts, yt_dlp, cookie_extra):
    entries = [e for e in (info.get("entries") or []) if e]
    title = safe_filename(info.get("title") or "playlist")
    folder = os.path.join(opts["out"], title)
    os.makedirs(folder, exist_ok=True)
    print(f"\n  Playlist: {title} - {len(entries)} item(s) -> {tilde(folder)}")

    failed, done = [], 0
    for i, entry in enumerate(entries, 1):
        eurl = entry.get("url") or entry.get("webpage_url") or entry.get("id")
        etitle = entry.get("title") or f"item {i}"
        print(f"\n  {'-' * 46}\n  [{i}/{len(entries)}] {etitle}")
        if not eurl:
            failed.append((i, etitle, "no URL in playlist entry", ""))
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="ipodmoviemaker-") as wd:
                media, mtitle = download_to(eurl, wd, prof, opts, yt_dlp,
                                            cookie_extra)
                if not media:
                    failed.append((i, etitle, "download failed", eurl))
                    continue
                if convert(media, folder, mtitle or etitle, prof, opts, wd):
                    done += 1
                else:
                    failed.append((i, etitle, "convert failed", eurl))
        except KeyboardInterrupt:
            print("\n  Skipped (Ctrl-C).")
            failed.append((i, etitle, "skipped by user", eurl))
        except Exception as e:
            failed.append((i, etitle, str(e)[:120], eurl))
            print(f"  {CROSS} Skipped - {str(e)[:150]}")

    print(f"\n  {done}/{len(entries)} on the iPod.")
    write_failed_manifest(folder, title, failed, len(entries))
    return done > 0


def handle_url(url, prof, opts, yt_dlp):
    def probe(extra):
        o = dict(_BASE_OPTS)
        o.update(extra or {})
        o["extract_flat"] = "in_playlist"
        with yt_dlp.YoutubeDL(o) as ydl:
            return ydl.extract_info(url, download=False)

    got = _extract(probe)
    if not got:
        return False
    info, cookie_extra = got
    if (info or {}).get("_type") == "playlist":
        return handle_playlist(url, info, prof, opts, yt_dlp, cookie_extra)

    with tempfile.TemporaryDirectory(prefix="ipodmoviemaker-") as wd:
        media, title = download_to(url, wd, prof, opts, yt_dlp, cookie_extra)
        if not media:
            return False
        return bool(convert(media, opts["out"], title, prof, opts, wd))


# ── Interactive prompts ───────────────────────────────────────────────────────
def ask_profile(device):
    print(f"\nThe {device['label']} panel is {SCREEN_W} x {SCREEN_H}. Nothing "
          "above 720p is visible on it.")
    for i, p in enumerate(PROFILES, 1):
        star = "  <- default" if p["key"] == DEFAULT_PROFILE else ""
        print(f"  [{i}] {p['label']:<15} ~{p['mb_per_hour']} MB/hr   "
              f"{p['note']}{star}")
    while True:
        ans = input(f"Profile [1-{len(PROFILES)}, Enter = default]: ").strip()
        if not ans:
            return profile_by_key(DEFAULT_PROFILE)
        if ans.isdigit() and 1 <= int(ans) <= len(PROFILES):
            return PROFILES[int(ans) - 1]
        print(f"  Enter 1-{len(PROFILES)}.")


def ask_codec(device):
    """Only ever called when `device["vcodecs"]` includes hevc - main()
    skips this prompt entirely on a device (touch5) that cannot decode it."""
    print(f"\nHEVC files are ~40% smaller and the {device['label']} still "
          "decodes them in hardware,\nbut they take longer to encode and need "
          "iOS 11+ to play at all.")
    while True:
        ans = input("Use HEVC instead of H.264? [y/N]: ").strip().lower()
        if ans in ("", "n", "no"):
            return "h264"
        if ans in ("y", "yes"):
            return "hevc"
        print("  Enter y or n.")


def collect_inputs(device):
    print(f"ipod-movie-maker - video for {device['label']}")
    print("Paste URLs, or drop in file/folder paths, one per line. "
          "Type 'done' when finished.")
    items = []
    while True:
        line = input(f"  {len(items) + 1}> ").strip().strip('"').strip("'")
        if line.lower() in ("done", "d", ""):
            if not items:
                print("Nothing to do.")
                sys.exit(1)
            return items
        items.append(os.path.expanduser(line))


# ── Entry point ───────────────────────────────────────────────────────────────
def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="ipod_movie_maker.py",
        description="Download or convert video into an iPod-touch-ready .m4v "
                     f"(default target: {DEVICES[DEFAULT_DEVICE]['label']}).",
        epilog="With no arguments it runs interactively.")
    ap.add_argument("inputs", nargs="*", help="URLs and/or local files/folders")
    ap.add_argument("--device", choices=sorted(DEVICES.keys()),
                    default=DEFAULT_DEVICE,
                    help="target device envelope - decides which codecs, "
                         f"profile and level are legal (default: {DEFAULT_DEVICE})")
    ap.add_argument("-p", "--profile", choices=[p["key"] for p in PROFILES],
                    help=f"quality ladder (default: {DEFAULT_PROFILE})")
    ap.add_argument("-c", "--codec", choices=["h264", "hevc"], default=None,
                    help="h264 = max compatibility, hevc = ~40%% smaller "
                         "(only decodable on --device touch7)")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT,
                    help=f"output folder (default: {tilde(DEFAULT_OUT)})")
    ap.add_argument("--ext", choices=["m4v", "mp4"], default="m4v",
                    help="output container extension (default: m4v, which the "
                         "Apple TV app imports most reliably)")
    ap.add_argument("--fast", action="store_true",
                    help="use the macOS hardware encoder: ~10x faster, "
                         "slightly worse per bit")
    ap.add_argument("--force-encode", action="store_true",
                    help="always transcode, even when the source already fits")
    ap.add_argument("--burn-subs", action="store_true",
                    help="paint subtitles into the picture (forces a transcode)")
    ap.add_argument("--no-subs", action="store_true", help="ignore subtitles")
    ap.add_argument("--subs-lang", default="en", help="subtitle language (en)")
    ap.add_argument("--subs-auto", action="store_true",
                    help="accept auto-generated subtitles too")
    ap.add_argument("--check", metavar="FILE",
                    help="report whether FILE plays on the selected --device, "
                         "then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the ffmpeg command instead of running it")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    require_ffmpeg()
    device = DEVICES[args.device]

    if args.codec == "hevc" and "hevc" not in device["vcodecs"]:
        # Fail fast for a scripted/non-interactive caller too - not just the
        # interactive ask_codec() prompt, which this device never even shows.
        raise SystemExit(
            f"--codec hevc was requested but {device['label']} has no HEVC "
            "decode path. Drop --codec, or pass --device touch7.")

    if args.check:
        path = os.path.expanduser(args.check)
        if not os.path.exists(path):
            raise SystemExit(f"No such file: {path}")
        print(f"\n{os.path.basename(path)}   "
              f"{human_size(os.path.getsize(path))}")
        ok = verify(path, device)
        msg = (f"{TICK} Plays on {device['label']}." if ok else
               f"{CROSS} Will not play (or not in hardware) - run it through "
               "ipod-movie-maker.")
        print(f"\n  {msg}")
        return 0 if ok else 1

    inputs = [os.path.expanduser(i) for i in args.inputs] or collect_inputs(device)
    prof = profile_by_key(args.profile) if args.profile else (
        profile_by_key(DEFAULT_PROFILE) if args.inputs else ask_profile(device))
    if args.codec:
        codec = args.codec
    elif args.inputs or "hevc" not in device["vcodecs"]:
        codec = "h264"          # no prompt - either non-interactive, or this
                                 # device has nothing to ask about
    else:
        codec = ask_codec(device)

    opts = {
        "codec": codec,
        "device": device,
        "out": os.path.expanduser(args.out),
        "ext": args.ext,
        "fast": args.fast,
        "force_encode": args.force_encode,
        "burn_subs": args.burn_subs,
        "no_subs": args.no_subs,
        "subs_lang": args.subs_lang,
        "subs_auto": args.subs_auto,
        "dry_run": args.dry_run,
    }

    needs_net = any(is_url(i) for i in inputs)
    yt_dlp = ensure_yt_dlp() if needs_net else None

    print(f"\n{device['label']}  ·  {prof['label']}  ·  "
          f"{'HEVC' if codec == 'hevc' else 'H.264'}  ·  -> {tilde(opts['out'])}")

    ok = 0
    for i, item in enumerate(inputs, 1):
        if len(inputs) > 1:
            print(f"\n{'=' * 60}\nItem {i}/{len(inputs)}: {item}")
        try:
            if is_url(item):
                done = handle_url(item, prof, opts, yt_dlp)
            elif os.path.exists(item):
                done = handle_local(item, prof, opts)
            else:
                print(f"  {CROSS} Not a URL and not a path that exists: {item}")
                done = False
            ok += bool(done)
        except KeyboardInterrupt:
            # Skip just this item; keep the queue going.
            print("\n  Skipped (Ctrl-C).")
            continue
        except Exception as e:
            # One bad input must never take the rest of the queue down with it.
            print(f"\n  {CROSS} Skipped - unexpected error: {str(e)[:200]}")
            continue

    print(f"\n{'=' * 60}")
    print(f"{ok}/{len(inputs)} item(s) ready in {tilde(opts['out'])}")
    print("Sync: drag into the Apple TV app on the Mac (it lands under Home "
          "Videos),\n      then plug the iPod in -> Finder -> the device -> "
          "Movies -> tick it -> Sync.")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
