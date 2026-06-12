#!/usr/bin/env python3
"""
Video Processing Service - Template Architecture
================================================
Stage 1 (SLOW - requires internet):
  Download + Pre-process each video into a reusable Template
  Template = Full 1080x1920 layout: blurred background + 3:4 cropped foreground overlaid

Stage 2 (FAST - fully offline):
  Load templates, apply Trim + 1.1x Speedup + Mirror + Concat
  Final output is exactly 61.0 seconds

Templates are cached in 'templates/' folder.
If a template already exists for a URL hash, Stage 1 is skipped entirely.
"""

import os
import sys
import math
import shutil
import hashlib
import tempfile
import argparse
import subprocess
import re
import json
import yt_dlp
import threading

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
TEMP_DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_processing")
TOTAL_DURATION = 61.0
SPEEDUP_FACTOR = 1.2

# ─────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────

def safe_print(msg, *args, **kwargs):
    """Safely print to stdout, ignoring OSErrors and encoding errors on Windows."""
    try:
        print(msg, *args, **kwargs)
    except UnicodeEncodeError:
        try:
            encoding = sys.stdout.encoding or "utf-8"
            encoded = str(msg).encode(encoding, errors="replace")
            print(encoded.decode(encoding), *args, **kwargs)
        except Exception:
            pass
    except OSError:
        pass

def url_to_hash(url: str) -> str:
    """Deterministic short hash for a URL (used as template filename)."""
    return hashlib.md5(url.strip().encode()).hexdigest()[:12]

def run_ffmpeg(args: list, description: str = "") -> subprocess.CompletedProcess:
    """Run an FFmpeg command silently. Raises RuntimeError on failure. If hardware encoding fails, falls back to libx264."""
    cmd = ["ffmpeg", "-y", "-nostdin"] + args
    proj_root = os.path.dirname(os.path.abspath(__file__))
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, stdin=subprocess.DEVNULL, cwd=proj_root)
    if res.returncode != 0:
        # Check if hardware encoder was used and we can fallback to libx264
        hw_encoders = ["h264_nvenc", "h264_qsv", "h264_amf", "h264_mf"]
        used_hw_encoder = None
        for enc in hw_encoders:
            if enc in args:
                used_hw_encoder = enc
                break
        
        if used_hw_encoder:
            safe_print(f"\n[WARNING] FFmpeg hardware encoding ({used_hw_encoder}) failed during: {description}. Retrying with CPU libx264 encoder...")
            fallback_args = list(args)
            try:
                c_idx = fallback_args.index("-c:v")
                pix_idx = fallback_args.index("-pix_fmt")
                fallback_opts = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-maxrate", "16M", "-bufsize", "32M", "-threads", "0"]
                fallback_args[c_idx:pix_idx] = fallback_opts
            except (ValueError, IndexError):
                # Fallback replacement failed, just try replacing the encoder name directly
                try:
                    enc_idx = fallback_args.index(used_hw_encoder)
                    fallback_args[enc_idx] = "libx264"
                except ValueError:
                    pass
            
            # Re-run with CPU encoding fallback
            fallback_cmd = ["ffmpeg", "-y", "-nostdin"] + fallback_args
            res = subprocess.run(fallback_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, stdin=subprocess.DEVNULL, cwd=proj_root)
            if res.returncode == 0:
                safe_print(f"✅ Hardware encoder fallback to libx264 succeeded!")
                return res
        
        # If it still failed or didn't use HW encoder
        safe_print(f"\n[ERROR] FFmpeg failed during: {description}")
        safe_print(res.stderr[-3000:])  # last 3000 chars of error
        err_msg = res.stderr[-500:].strip()
        raise RuntimeError(f"FFmpeg failed ({description}): {err_msg}")
    return res

def run_ffprobe(args: list) -> str:
    """Run ffprobe and return stdout. Raises on failure."""
    cmd = ["ffprobe"] + args
    proj_root = os.path.dirname(os.path.abspath(__file__))
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, stdin=subprocess.DEVNULL, cwd=proj_root)
        if res.returncode != 0:
            with open("ffprobe_debug.log", "a", encoding="utf-8") as f_deb:
                f_deb.write(f"CMD: {cmd}\nRC: {res.returncode}\nSTDERR: {res.stderr}\nSTDOUT: {res.stdout}\n---\n")
        return res.stdout.strip()
    except Exception as e:
        with open("ffprobe_debug.log", "a", encoding="utf-8") as f_deb:
            f_deb.write(f"CMD: {cmd}\nEXCEPTION: {str(e)}\n---\n")
        raise e

def get_duration(path: str) -> float:
    """Get video duration in seconds via ffprobe (Windows-safe)."""
    # Method 1: format duration
    out = run_ffprobe(["-v", "error", "-show_entries", "format=duration",
                       "-of", "default=noprint_wrappers=1:nokey=1", path])
    # Strip whitespace / \r\n and take only the first non-empty token
    out = out.strip().split()[0] if out and out.strip() else ""
    try:
        val = float(out)
        if val > 0:
            return val
    except (ValueError, IndexError):
        pass

    # Method 2: stream duration
    out2 = run_ffprobe(["-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", path])
    out2 = out2.strip().split()[0] if out2 and out2.strip() else ""
    try:
        val2 = float(out2)
        if val2 > 0:
            return val2
    except (ValueError, IndexError):
        pass

    # Method 3: parse ffmpeg stderr (last resort)
    try:
        proj_root = os.path.dirname(os.path.abspath(__file__))
        res = subprocess.run(
            ["ffmpeg", "-i", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, stdin=subprocess.DEVNULL, cwd=proj_root
        )
        for line in res.stderr.splitlines():
            if "Duration:" in line:
                m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", line)
                if m:
                    h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                    return h * 3600 + mn * 60 + s
    except Exception:
        pass

    raise ValueError(f"Cannot determine duration of: {path}")

def get_video_info(path: str) -> dict:
    """Return dict with width, height, duration."""
    out = run_ffprobe(["-v", "error", "-select_streams", "v:0",
                       "-show_entries", "stream=width,height",
                       "-of", "json", path])
    data = json.loads(out)
    stream = data["streams"][0]
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "duration": get_duration(path),
    }

def get_video_codec(path: str) -> str:
    """Return the codec name of the first video stream."""
    try:
        out = run_ffprobe(["-v", "error", "-select_streams", "v:0",
                           "-show_entries", "stream=codec_name",
                           "-of", "default=noprint_wrappers=1:nokey=1", path])
        out = out.strip().split()[0] if out and out.strip() else ""
        return out.lower()
    except Exception:
        return ""

def extract_thumbnail(video_path: str, thumb_path: str):
    """Extract a single frame thumbnail from a video file using FFmpeg."""
    cmd = [
        "ffmpeg", "-y", "-nostdin",
        "-ss", "1.0",
        "-i", video_path,
        "-vframes", "1",
        "-vf", "scale=360:-1",
        "-f", "image2",
        thumb_path
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, stdin=subprocess.DEVNULL)
    except Exception:
        pass

def should_mirror(index: int, N: int) -> bool:
    """Mirror rule per spec."""
    if N == 1: return False
    if N == 2: return index == 1
    if N == 3: return index == 1
    if N == 4: return index in (1, 3)
    if N == 5: return index in (0, 4)
    return False

# Cache for the best H264 encoder
_BEST_ENCODER_CACHE = None

def get_best_h264_encoder() -> tuple[str, list]:
    """
    Detect the best available H264 encoder on the system and return (encoder_name, default_opts).
    Caches the result so it only probes once per process.
    """
    global _BEST_ENCODER_CACHE
    if _BEST_ENCODER_CACHE is not None:
        return _BEST_ENCODER_CACHE

    candidates = [
        ("h264_nvenc", ["-preset", "p1", "-rc", "constqp", "-qp", "16", "-maxrate", "16M", "-bufsize", "32M"]),
        ("h264_qsv", ["-preset", "veryfast", "-global_quality", "16", "-maxrate", "16M", "-bufsize", "32M"]),
        ("h264_amf", ["-rc", "cqp", "-qp_i", "16", "-qp_p", "16", "-maxrate", "16M", "-bufsize", "32M"]),
        ("h264_mf", ["-rate_control", "3", "-quality", "16", "-maxrate", "16M", "-bufsize", "32M"]),
        ("libx264", ["-preset", "ultrafast", "-crf", "16", "-maxrate", "16M", "-bufsize", "32M", "-threads", "0"])
    ]

    proj_root = os.path.dirname(os.path.abspath(__file__))
    for encoder, opts in candidates:
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
               "-c:v", encoder, "-t", "1", "-f", "null", "-"]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, stdin=subprocess.DEVNULL, cwd=proj_root)
            if res.returncode == 0:
                _BEST_ENCODER_CACHE = (encoder, opts)
                return _BEST_ENCODER_CACHE
        except Exception:
            continue

    # Fallback to libx264 CPU encoding
    _BEST_ENCODER_CACHE = ("libx264", ["-preset", "ultrafast", "-crf", "16", "-maxrate", "16M", "-bufsize", "32M", "-threads", "0"])
    return _BEST_ENCODER_CACHE

# ─────────────────────────────────────────────
# Stage 1 — Download
# ─────────────────────────────────────────────

def _find_downloaded_file(info: dict, out_dir: str, url: str) -> str:
    """Helper to locate the downloaded video file in the output directory."""
    # 1. Try requested_downloads (most reliable path)
    if "requested_downloads" in info and info["requested_downloads"]:
        fp = info["requested_downloads"][0].get("filepath", "")
        if fp and os.path.exists(fp):
            return fp

    # 2. Construct expected path from video ID
    video_id = info.get("id", "unknown")
    expected = os.path.join(out_dir, f"raw_{video_id}.mp4")
    if os.path.exists(expected):
        return expected

    # 3. Scan out_dir for any mp4/mkv/webm file created during this run
    for fname in os.listdir(out_dir):
        if fname.startswith("raw_") and fname.endswith((".mp4", ".mkv", ".webm")):
            return os.path.join(out_dir, fname)

    raise FileNotFoundError(f"Downloaded file not found for URL: {url}")

def download_video(url: str, out_dir: str, idx: int = 0) -> tuple:
    """Download a single URL. Returns local file path and info dict."""
    import re
    # Find the first HTTP/HTTPS URL in the string (in case user pasted extra text)
    url_match = re.search(r'https?://[^\s/$.?#].[^\s]*', url)
    if not url_match:
        raise ValueError(f"Invalid URL: '{url}'. No HTTP/HTTPS link found in input.")
    
    url_stripped = url_match.group(0).rstrip(')(')
    
    # Strip any query parameters (like ?is_from_webapp=...) to avoid yt-dlp rehydration error
    if '?' in url_stripped:
        url_stripped = url_stripped.split('?')[0]

    import traceback as _tb

    # ── TikTok Routes rotation ─────────────────────────────────────────────
    if "tiktok.com" in url_stripped:
        import ssl
        ssl_ctx = ssl._create_unverified_context()
        routes = ["tikwm", "lovetik"] if idx % 2 == 0 else ["lovetik", "tikwm"]
        for route in routes:
            if route == "tikwm":
                try:
                    import urllib.request
                    import urllib.parse
                    safe_print(f"🚀 Trying Route 1 (TikWM API) for TikTok: {url_stripped}")
                    api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url_stripped)}"
                    api_req = urllib.request.Request(api_url, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    })
                    with urllib.request.urlopen(api_req, timeout=7.0, context=ssl_ctx) as resp:
                        data = json.loads(resp.read().decode('utf-8'))
                    
                    if data.get("code") == 0 and "data" in data:
                        video_data = data["data"]
                        video_id = video_data.get("id") or url_to_hash(url_stripped)
                        play_url = video_data.get("play") or video_data.get("wmplay")
                        if play_url:
                            dest_path = os.path.join(out_dir, f"raw_{video_id}.mp4")
                            safe_print(f"📥 Downloading direct video stream from TikWM CDN...")
                            video_req = urllib.request.Request(play_url, headers={
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                            })
                            with urllib.request.urlopen(video_req, timeout=20, context=ssl_ctx) as video_resp:
                                with open(dest_path, "wb") as f_out:
                                    shutil.copyfileobj(video_resp, f_out, length=1024*1024)
                            
                            if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
                                if os.path.exists(dest_path):
                                    try: os.remove(dest_path)
                                    except: pass
                                raise ValueError("TikWM downloaded an empty file")
                                
                            info = {
                                "id": video_id,
                                "title": video_data.get("title", ""),
                                "description": video_data.get("title", ""),
                                "view_count": video_data.get("play_count", 0),
                                "like_count": video_data.get("digg_count", 0),
                                "uploader": video_data.get("author", {}).get("unique_id", ""),
                                "requested_downloads": [{"filepath": dest_path}]
                            }
                            safe_print(f"✅ TikWM download complete!")
                            return dest_path, info
                except Exception as e:
                    safe_print(f"⚠️ TikWM Route failed: {e}")
            
            elif route == "lovetik":
                try:
                    import urllib.request
                    import urllib.parse
                    safe_print(f"🚀 Trying Route 2 (Lovetik API) for TikTok: {url_stripped}")
                    api_url = "https://lovetik.com/api/ajax/search"
                    post_data = urllib.parse.urlencode({'query': url_stripped}).encode('utf-8')
                    req = urllib.request.Request(api_url, data=post_data, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
                    })
                    
                    with urllib.request.urlopen(req, timeout=7.0, context=ssl_ctx) as resp:
                        res = json.loads(resp.read().decode('utf-8'))
                    
                    if res.get('status') == 'ok' and 'links' in res:
                        links = res['links']
                        best_link = None
                        for quality in ["HD Original", "720p", "576p"]:
                            for link in links:
                                if quality in link.get('s', '') and link.get('a'):
                                    best_link = link.get('a')
                                    break
                            if best_link:
                                break
                        
                        if not best_link:
                            for link in links:
                                if 'Watermarked' not in link.get('s', '') and link.get('a'):
                                    best_link = link.get('a')
                                    break
                                    
                        if not best_link and links:
                            best_link = links[0].get('a')
                            
                        if best_link:
                            video_id = res.get('id') or url_to_hash(url_stripped)
                            dest_path = os.path.join(out_dir, f"raw_{video_id}.mp4")
                            safe_print(f"📥 Downloading direct video stream from Lovetik CDN...")
                            video_req = urllib.request.Request(best_link, headers={
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                            })
                            with urllib.request.urlopen(video_req, timeout=20, context=ssl_ctx) as video_resp:
                                with open(dest_path, "wb") as f_out:
                                    shutil.copyfileobj(video_resp, f_out, length=1024*1024)
                            
                            if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
                                if os.path.exists(dest_path):
                                    try: os.remove(dest_path)
                                    except: pass
                                raise ValueError("Lovetik downloaded an empty file")
                                
                            info = {
                                "id": video_id,
                                "title": res.get("desc", ""),
                                "description": res.get("desc", ""),
                                "view_count": 0,
                                "like_count": 0,
                                "uploader": res.get("author", "").replace("@", ""),
                                "requested_downloads": [{"filepath": dest_path}]
                            }
                            safe_print(f"✅ Lovetik download complete!")
                            return dest_path, info
                except Exception as e:
                    safe_print(f"⚠️ Lovetik Route failed: {e}")

        safe_print("⚠️ All TikTok fast-download APIs failed. Falling back to yt-dlp...")

    import traceback as _tb

    # ── Common yt-dlp options ──────────────────────────────────────────────
    class MyLogger:
        def debug(self, msg):
            pass
        def warning(self, msg):
            pass
        def error(self, msg):
            pass

    def _make_opts(browser=None):
        opts = {
            "format": "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=avc1]+bestaudio/best[vcodec^=avc1]/best[ext=mp4]/best",
            "outtmpl": os.path.join(out_dir, "raw_%(id)s.%(ext)s"),
            "nopart": True,
            "restrictfilenames": True,
            "windowsfilenames": True,
            "cachedir": False,
            "nocheckcertificate": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": MyLogger(),
            "socket_timeout": 4,  # Fail fast if connection hangs
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1"
            }
        }
        if browser:
            opts["cookiesfrombrowser"] = (browser, None)
        return opts

    # ── Try no cookies first (fastest and works 99% of the time) ────────────
    try:
        with yt_dlp.YoutubeDL(_make_opts()) as ydl:
            info = ydl.extract_info(url, download=True)
            fp = _find_downloaded_file(info, out_dir, url)
            if os.path.exists(fp) and os.path.getsize(fp) > 0:
                return fp, info
            raise ValueError("yt-dlp downloaded empty file")
    except Exception:
        pass

    # ── Fallback 1: Try common browsers (Chrome, Edge) ──────────────────────
    for browser in ["chrome", "edge"]:
        try:
            with yt_dlp.YoutubeDL(_make_opts(browser)) as ydl:
                info = ydl.extract_info(url, download=True)
                fp = _find_downloaded_file(info, out_dir, url)
                if os.path.exists(fp) and os.path.getsize(fp) > 0:
                    return fp, info
                raise ValueError("yt-dlp browser downloaded empty file")
        except Exception:
            continue

    # yt-dlp fallback finished

    # ── Final fallback failed ──────────────────────────────────────────────
    try:
        print("--- YT-DLP ALL ATTEMPTS FAILED ---")
        _tb.print_exc()
    except OSError:
        pass
    raise Exception(f"Failed to download video from {url} after trying all cookie fallbacks.")

# ─────────────────────────────────────────────
# Stage 1 — Pre-process into Template
# ─────────────────────────────────────────────

def build_template(raw_path: str, template_path: str, progress_cb=None):
    """
    Convert a raw downloaded video into a reusable 1080x1920 template.
    Template = blurred background + 3:4 cropped foreground overlaid, NO audio, 30fps.
    The template keeps the FULL original duration (no trimming yet).
    Trimming and speedup happen cheaply at compile time.
    """
    if progress_cb:
        progress_cb("🎨 Building layout template (blur + crop + overlay)...")

    # Background: scale down to tiny size (32x56), crop, and scale back up to 1080x1920 (creates natural fast blur)
    bg_filter = (
        "scale=w='2*trunc(max(32,56*in_w/in_h)/2)'"
        ":h='2*trunc(max(56,32*in_h/in_w)/2)',"
        "crop=32:56,"
        "scale=1080:1920"
    )

    # Foreground: crop to 3:4 from center, scale to fit width=1080 → 1080x1440
    fg_filter = (
        "crop=w='min(in_w,in_h*3/4)':h='min(in_h,in_w*4/3)',"
        "scale=1080:1440"
    )

    filter_complex = (
        f"[0:v]split=2[bg_raw][fg_raw];"
        f"[bg_raw]{bg_filter}[bg];"
        f"[fg_raw]{fg_filter}[fg];"
        f"[bg][fg]overlay=x=0:y=240,setsar=1[out]"
    )

    encoder, encoder_opts = get_best_h264_encoder()

    run_ffmpeg([
        "-i", raw_path,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-an",
        "-r", "30",
        "-c:v", encoder
    ] + encoder_opts + [
        "-pix_fmt", "yuv420p",
        template_path
    ], description="template encoding")

def get_or_build_template(url: str, progress_cb=None, idx: int = 0) -> str:
    """
    Return path to cached template for a URL.
    If not cached, download + build and cache it.
    """
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    os.makedirs(TEMP_DOWNLOADS_DIR, exist_ok=True)

    url_hash = url_to_hash(url)
    template_path = os.path.join(TEMPLATES_DIR, f"tpl_{url_hash}.mp4")

    if os.path.exists(template_path) and os.path.getsize(template_path) > 0:
        if progress_cb:
            progress_cb(f"✅ Template already cached (hash: {url_hash})")
        return template_path
    elif os.path.exists(template_path):
        try: os.remove(template_path)
        except Exception: pass

    # Need to download and build
    if progress_cb:
        progress_cb(f"📥 Downloading video from: {url}")

    tmp_dir = tempfile.mkdtemp(dir=TEMP_DOWNLOADS_DIR, prefix="dl_")
    try:
        raw_path, info = download_video(url, tmp_dir, idx=idx)
        if progress_cb:
            progress_cb(f"✅ Download complete. Building template...")
        build_template(raw_path, template_path, progress_cb)
        
        # Save metadata JSON
        if info:
            metadata_path = os.path.join(TEMPLATES_DIR, f"tpl_{url_hash}.json")
            metadata = {
                "title": info.get("title", ""),
                "description": info.get("description", ""),
                "view_count": info.get("view_count", 0),
                "like_count": info.get("like_count", 0),
                "uploader": info.get("uploader", ""),
            }
            try:
                with open(metadata_path, "w", encoding="utf-8") as f_meta:
                    json.dump(metadata, f_meta, ensure_ascii=False, indent=2)
            except Exception as meta_err:
                if progress_cb:
                    progress_cb(f"⚠️ Warning: Could not save metadata JSON: {meta_err}")
                    
        if progress_cb:
            progress_cb(f"💾 Template saved: tpl_{url_hash}.mp4")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return template_path

def get_or_download_raw_video(url: str, progress_cb=None, progress_percent_cb=None, idx: int = 0) -> str:
    """
    Return path to cached raw video for a URL.
    If not cached, download and cache it.
    Also saves metadata JSON.
    """
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    os.makedirs(TEMP_DOWNLOADS_DIR, exist_ok=True)

    url_hash = url_to_hash(url)
    raw_path = os.path.join(TEMPLATES_DIR, f"raw_{url_hash}.mp4")

    if os.path.exists(raw_path) and os.path.getsize(raw_path) > 0:
        try:
            dur = get_duration(raw_path)
            if dur > 0:
                if progress_percent_cb:
                    progress_percent_cb(100)
                if progress_cb:
                    progress_cb(f"✅ Video already downloaded (hash: {url_hash})")
                return raw_path
        except Exception:
            pass
            
    # Delete if exists but invalid/corrupted (either 0 size or get_duration failed)
    if os.path.exists(raw_path):
        try: os.remove(raw_path)
        except Exception: pass

    # Need to download
    if progress_percent_cb:
        progress_percent_cb(10)
    if progress_cb:
        progress_cb(f"📥 Downloading video from: {url}")

    tmp_dir = tempfile.mkdtemp(dir=TEMP_DOWNLOADS_DIR, prefix="dl_")
    try:
        if progress_percent_cb:
            progress_percent_cb(30)
        downloaded_raw, info = download_video(url, tmp_dir, idx=idx)
        if progress_percent_cb:
            progress_percent_cb(80)
        if progress_cb:
            progress_cb(f"✅ Download complete. Saving video and extracting thumbnail...")
        
        # Verify the downloaded video is valid and has a readable duration
        try:
            dur = get_duration(downloaded_raw)
            if dur <= 0:
                raise ValueError("Video duration is zero or negative.")
        except Exception as dur_err:
            raise ValueError(f"Downloaded video is invalid or corrupted (could not parse duration): {dur_err}")

        # Copy original 1080p video directly (no transcoding!)
        shutil.copy2(downloaded_raw, raw_path)
        
        # Extract a single frame thumbnail image for Stage 2 preview
        thumb_path = os.path.splitext(raw_path)[0] + "_thumb.jpg"
        extract_thumbnail(raw_path, thumb_path)
        
        # Save metadata JSON using raw prefix to match
        if info:
            metadata_path = os.path.join(TEMPLATES_DIR, f"raw_{url_hash}.json")
            metadata = {
                "title": info.get("title", ""),
                "description": info.get("description", ""),
                "view_count": info.get("view_count", 0),
                "like_count": info.get("like_count", 0),
                "uploader": info.get("uploader", ""),
            }
            try:
                with open(metadata_path, "w", encoding="utf-8") as f_meta:
                    json.dump(metadata, f_meta, ensure_ascii=False, indent=2)
            except Exception as meta_err:
                if progress_cb:
                    progress_cb(f"⚠️ Warning: Could not save metadata JSON: {meta_err}")
                    
        if progress_percent_cb:
            progress_percent_cb(100)
        if progress_cb:
            progress_cb(f"💾 Raw video saved: raw_{url_hash}.mp4")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return raw_path

import concurrent.futures

def download_all_raw_videos_async(urls: list, progress_cb=None, progress_percent_cb=None) -> list:
    """
    Download raw videos for multiple URLs asynchronously.
    Returns a list of local raw video file paths matching the input URL order.
    """
    N = len(urls)
    raw_paths = [None] * N
    
    progress_dict = {i: 0 for i in range(N)}
    lock = threading.Lock()
    
    def update_overall_progress(idx, percent):
        with lock:
            progress_dict[idx] = percent
            avg_percent = sum(progress_dict.values()) // N
            if progress_percent_cb:
                progress_percent_cb(avg_percent, f"جاري تحميل الفيديوهات... {avg_percent}%")

    def worker(idx, url):
        # Stagger start to prevent concurrent API rate limits
        if idx > 0:
            import time
            time.sleep(idx * 0.4)
        def local_cb(msg):
            if progress_cb:
                progress_cb(f"[{idx+1}/{N}] {msg}")
        def local_percent_cb(pct):
            update_overall_progress(idx, pct)
        return get_or_download_raw_video(url, progress_cb=local_cb, progress_percent_cb=local_percent_cb, idx=idx)

    if progress_percent_cb:
        progress_percent_cb(0, "جاري بدء التحميل... 0%")

    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as executor:
        future_to_idx = {executor.submit(worker, idx, url): idx for idx, url in enumerate(urls)}
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                raw_paths[idx] = future.result()
            except Exception as e:
                import traceback
                with open("download_error.log", "w", encoding="utf-8") as f_err:
                    traceback.print_exc(file=f_err)
                if progress_cb:
                    progress_cb(f"❌ Video {idx+1} failed: {e}")
                raise e

    return raw_paths

def build_all_templates_async(urls: list, progress_cb=None) -> list:
    """
    Build templates for multiple URLs asynchronously.
    Returns a list of local template file paths matching the input URL order.
    """
    N = len(urls)
    template_paths = [None] * N

    def worker(idx, url):
        # Stagger start to prevent concurrent API rate limits
        if idx > 0:
            import time
            time.sleep(idx * 0.4)
        def local_cb(msg):
            if progress_cb:
                progress_cb(f"[{idx+1}/{N}] {msg}")
        return get_or_build_template(url, progress_cb=local_cb, idx=idx)

    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as executor:
        future_to_idx = {executor.submit(worker, idx, url): idx for idx, url in enumerate(urls)}
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                template_paths[idx] = future.result()
            except Exception as e:
                import traceback
                with open("download_error.log", "w", encoding="utf-8") as f_err:
                    traceback.print_exc(file=f_err)
                if progress_cb:
                    progress_cb(f"❌ Video {idx+1} failed: {e}")
                raise e

    return template_paths

# ─────────────────────────────────────────────
# Stage 2 — Fast Compile from Templates
# ─────────────────────────────────────────────

def compile_from_templates(template_paths: list, output_path: str, progress_cb=None):
    """
    Stage 2: Compile final video directly from raw video paths.
    Applies per-segment: Trim → 1.2x Speedup → Mirror (optional) → Layout (Blur background + 3:4 crop foreground overlay) → Concat
    Total duration: exactly TOTAL_DURATION seconds.
    """
    N = len(template_paths)
    segment_duration = TOTAL_DURATION / N          # final duration per segment
    source_duration  = segment_duration * SPEEDUP_FACTOR  # input duration needed

    if progress_cb:
        progress_cb(f"⚡ Compiling {N} raw video(s) → {TOTAL_DURATION}s output")
        progress_cb(f"   Each segment: {segment_duration:.3f}s | Source needed: {source_duration:.3f}s")

    ffmpeg_args = []
    filter_parts = []
    concat_labels = []

    # Add inputs with optional loop if raw video is too short
    for idx, raw in enumerate(template_paths):
        raw_duration = get_duration(raw)
        if raw_duration < source_duration:
            loop_count = math.ceil(source_duration / raw_duration) - 1
            ffmpeg_args += ["-stream_loop", str(loop_count)]
            if progress_cb:
                progress_cb(f"   Video {idx+1}: {raw_duration:.1f}s → looping {loop_count}x to cover {source_duration:.1f}s")
        else:
            if progress_cb:
                progress_cb(f"   Video {idx+1}: {raw_duration:.1f}s ✓")
        ffmpeg_args += ["-i", raw]

    # Build per-segment filters: trim → speedup → optional mirror → layout (blur background + crop foreground overlay)
    for idx in range(N):
        mirror = should_mirror(idx, N)
        seg_label = f"seg{idx}"

        # Trim, Speedup, Setsar, and Mirror (optional)
        base_ops = (
            f"[{idx}:v]"
            f"trim=0:{source_duration:.6f},"
            f"setpts=(PTS-STARTPTS)/{SPEEDUP_FACTOR},"
            f"setsar=1"
        )
        if mirror:
            base_ops += ",hflip"
            
        base_label = f"base{idx}"
        filter_parts.append(f"{base_ops}[{base_label}]")
        
        # Split base for background and foreground
        filter_parts.append(f"[{base_label}]split=2[bg_raw{idx}][fg_raw{idx}]")
        
        # Background: scale down to tiny size (32x56), crop, and scale back up to 1080x1920 (creates natural fast blur)
        bg_filter = (
            "scale=w='2*trunc(max(32,56*in_w/in_h)/2)'"
            ":h='2*trunc(max(56,32*in_h/in_w)/2)',"
            "crop=32:56,"
            "scale=1080:1920"
        )
        filter_parts.append(f"[bg_raw{idx}]{bg_filter}[bg{idx}]")
        
        # Foreground: crop to 3:4 from center, scale to fit width=1080 → 1080x1440
        fg_filter = (
            "crop=w='min(in_w,in_h*3/4)':h='min(in_h,in_w*4/3)',"
            "scale=1080:1440"
        )
        filter_parts.append(f"[fg_raw{idx}]{fg_filter}[fg{idx}]")
        
        # Overlay foreground on background
        filter_parts.append(f"[bg{idx}][fg{idx}]overlay=x=0:y=240,setsar=1[{seg_label}]")
        concat_labels.append(f"[{seg_label}]")

    # Final concat of the processed segments
    concat_str = "".join(concat_labels)
    filter_parts.append(f"{concat_str}concat=n={N}:v=1:a=0[outv]")

    filter_complex = ";".join(filter_parts)

    if progress_cb:
        progress_cb("🔗 Processing final video layout (blur + crop + overlay per segment)...")

    encoder, encoder_opts = get_best_h264_encoder()

    run_ffmpeg(
        ffmpeg_args + [
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-an",
            "-r", "30",
            "-c:v", encoder
        ] + encoder_opts + [
            "-pix_fmt", "yuv420p",
            "-t", str(TOTAL_DURATION),
            output_path
        ],
        description="compile directly from raw videos with layout per segment"
    )

    if progress_cb:
        try:
            actual = get_duration(output_path)
            progress_cb(f"✅ Output verified: {actual:.4f}s | Resolution: 1080×1920")
        except Exception:
            progress_cb("✅ Output file created.")

def extract_audio(input_path: str, output_path: str):
    """Extract audio from a video file and save as MP3."""
    run_ffmpeg([
        "-i", input_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        output_path
    ], description="extracting audio")

_cached_encoder = None

def detect_fastest_encoder(video_path: str) -> list:
    global _cached_encoder
    if _cached_encoder is not None:
        return _cached_encoder
        
    encoders = [
        ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "24"]),
        ("h264_qsv", ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "24"]),
        ("h264_mf", ["-c:v", "h264_mf", "-quality", "1"]),
        ("libx264_ultrafast", ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "22"])
    ]
    
    import tempfile
    temp_out = os.path.join(tempfile.gettempdir(), f"test_enc_detect_{int(hashlib.md5(video_path.encode()).hexdigest()[:6], 16)}.mp4")
    
    for name, args in encoders:
        test_cmd = [
            "ffmpeg", "-y",
            "-ss", "0",
            "-i", video_path,
            "-t", "0.1"
        ] + args + [
            "-an",
            temp_out
        ]
        try:
            proj_root = os.path.dirname(os.path.abspath(__file__))
            res = subprocess.run(test_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1.5, cwd=proj_root)
            if res.returncode == 0:
                if os.path.exists(temp_out):
                    try: os.remove(temp_out)
                    except: pass
                _cached_encoder = args
                return args
        except Exception:
            pass
            
    # Fallback
    _cached_encoder = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "22"]
    return _cached_encoder

def mix_audio_to_video(video_path: str, audio_path: str, output_path: str, clips: list):
    """
    Mix multiple audio clips onto a video.
    Each clip dict: {"trim_start": float, "trim_end": float, "delay_sec": float, "volume": float}
    Forces output to exactly 61 seconds.
    Video track is copied directly (-c:v copy) for instantaneous merge and 100% quality.
    """
    M = len(clips)
    
    if M == 0:
        # If no clips, output video without audio
        run_ffmpeg([
            "-i", video_path,
            "-map", "0:v",
            "-c:v", "copy",
            "-an",
            "-t", str(TOTAL_DURATION),
            output_path
        ], description="copy video without audio")
        return

    filter_parts = []
    
    # 1. Split the audio input if there are multiple clips
    if M > 1:
        split_outputs = "".join(f"[a{i}_raw]" for i in range(M))
        filter_parts.append(f"[1:a]asplit={M}{split_outputs}")
    
    # 2. Build trim, volume, and delay filter for each clip
    mix_inputs = []
    for i, clip in enumerate(clips):
        t_start = clip["trim_start"]
        t_end = clip["trim_end"]
        vol = clip["volume"]
        delay_ms = int(clip["delay_sec"] * 1000)
        
        input_label = f"[a{i}_raw]" if M > 1 else "[1:a]"
        output_label = f"[a{i}_out]"
        
        filter_parts.append(
            f"{input_label}atrim=start={t_start:.6f}:end={t_end:.6f},"
            f"asetpts=PTS-STARTPTS,"
            f"volume={vol:.2f},"
            f"adelay={delay_ms}|{delay_ms}{output_label}"
        )
        mix_inputs.append(output_label)
        
    # 3. Mix the processed clips
    if M > 1:
        inputs_str = "".join(mix_inputs)
        filter_parts.append(f"{inputs_str}amix=inputs={M}:dropout_transition=0:normalize=0[a_mixed]")
        final_audio_label = "[a_mixed]"
    else:
        final_audio_label = mix_inputs[0]
        
    filter_complex = ";".join(filter_parts)
    
    # Copy video stream and mix audio
    run_ffmpeg([
        "-i", video_path,
        "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", final_audio_label,
        "-c:v", "copy",
        "-c:a", "aac",
        "-t", str(TOTAL_DURATION),
        output_path
    ], description="multiple audio mixing")


# ─────────────────────────────────────────────
# Main CLI entry point
# ─────────────────────────────────────────────

def main():
    global TEMPLATES_DIR   # must be first — before any read of TEMPLATES_DIR
    parser = argparse.ArgumentParser(
        description="Video Processing Studio — Template-based pipeline.\n"
                    "Stage 1 (internet): Download + pre-process templates.\n"
                    "Stage 2 (offline):  Trim + speedup + mirror + compile.\n",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("urls", nargs="+",
                        help="1–5 video URLs (TikTok, Shorts, Reels, Facebook)")
    parser.add_argument("-o", "--output", default="final_output.mp4",
                        help="Output file path (default: final_output.mp4)")
    parser.add_argument("--templates-dir", default=None,
                        help="Directory for cached templates (default: ./templates/)")
    parser.add_argument("--list-templates", action="store_true",
                        help="List all cached templates and exit")
    parser.add_argument("--clear-templates", action="store_true",
                        help="Delete all cached templates and exit")

    args = parser.parse_args()
    if args.templates_dir:
        TEMPLATES_DIR = os.path.abspath(args.templates_dir)

    # Handle utility commands
    if args.list_templates:
        files = [f for f in os.listdir(TEMPLATES_DIR) if f.endswith(".mp4")] if os.path.exists(TEMPLATES_DIR) else []
        print(f"Cached templates ({len(files)}) in {TEMPLATES_DIR}:")
        for f in files:
            size = os.path.getsize(os.path.join(TEMPLATES_DIR, f)) / (1024*1024)
            print(f"  {f}  ({size:.1f} MB)")
        return

    if args.clear_templates:
        if os.path.exists(TEMPLATES_DIR):
            shutil.rmtree(TEMPLATES_DIR)
            print(f"Cleared all templates in: {TEMPLATES_DIR}")
        return

    if not (1 <= len(args.urls) <= 5):
        print("Error: Please provide between 1 and 5 URLs.", file=sys.stderr)
        sys.exit(1)

    print("=" * 55)
    print("  Video Processing Studio — Template Pipeline")
    print("=" * 55)

    def log(msg):
        safe_print(f"  {msg}")

    # Stage 1: Ensure all templates are built (asynchronously)
    print("\n[Stage 1] Building / Loading Templates (Asynchronously)")
    print("-" * 55)
    template_paths = build_all_templates_async(args.urls, progress_cb=log)

    # Stage 2: Fast compile
    print("\n[Stage 2] Fast Compilation")
    print("-" * 55)
    output_path = os.path.abspath(args.output)
    compile_from_templates(template_paths, output_path, progress_cb=log)

    print("\n" + "=" * 55)
    print(f"  Done! Output saved to:")
    print(f"  {output_path}")
    print("=" * 55)

if __name__ == "__main__":
    main()
