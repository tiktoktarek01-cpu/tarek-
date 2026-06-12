import os
import threading
import queue
import time
import streamlit as st
import streamlit.components.v1 as components
try:
    import yt_dlp
    import sys
    with open("system_info.log", "w", encoding="utf-8") as f_sys:
        f_sys.write(f"Python: {sys.version}\n")
        f_sys.write(f"yt-dlp: {yt_dlp.__version__}\n")
        f_sys.write(f"Executable: {sys.executable}\n")
        f_sys.write(f"Path: {sys.path}\n")
except Exception:
    pass

import importlib
import video_processor
importlib.reload(video_processor)

from video_processor import (
    build_all_templates_async,
    compile_from_templates,
    get_duration,
    TEMPLATES_DIR,
    url_to_hash,
    SPEEDUP_FACTOR,
    extract_audio,
    mix_audio_to_video,
    download_all_raw_videos_async,
    get_or_download_raw_video,
    build_template,
    get_video_codec,
    extract_thumbnail,
)
import json

def cleanup_temporary_files(force: bool = False, clean_root: bool = False):
    """
    Deletes all temporary files in 'temp_processing' folder.
    If force is False, only deletes files older than 30 minutes to prevent deleting active files.
    If force is True, deletes all files.
    If clean_root is True, also deletes root compiled video files.
    """
    temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_processing")
    if os.path.exists(temp_dir):
        now = time.time()
        for fname in os.listdir(temp_dir):
            fpath = os.path.join(temp_dir, fname)
            if os.path.isdir(fpath):
                continue
            if not force:
                try:
                    mtime = os.path.getmtime(fpath)
                    if now - mtime < 1800:
                        continue
                except Exception:
                    pass
            try:
                os.remove(fpath)
            except Exception:
                pass

    if clean_root:
        root_files = [
            "compiled_output.mp4",
            "compiled_with_audio.mp4",
            "compiled_with_voiceover.mp4",
            "compiled_studio_video.mp4"
        ]
        for f in root_files:
            p = os.path.abspath(f)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

# Run passive cleanup on startup/reload
cleanup_temporary_files(force=False)


# ─── Persistent API Key Storage ───────────────────────────────────────────

_KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_keys_config.json")

def _load_saved_api_keys() -> dict:
    """Load API keys from local JSON file (survives restarts)."""
    try:
        if os.path.exists(_KEYS_FILE):
            with open(_KEYS_FILE, "r", encoding="utf-8") as _f:
                return json.load(_f)
    except Exception:
        pass
    return {}

def _persist_api_key(key_type: str, value: str):
    """Save or remove an API key on disk. key_type: 'openrouter' or 'elevenlabs'"""
    try:
        data = _load_saved_api_keys()
        if value:
            data[key_type] = value
        else:
            data.pop(key_type, None)
        with open(_KEYS_FILE, "w", encoding="utf-8") as _f:
            json.dump(data, _f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def get_custom_progress_bar_html(percent, label):
    return f"""
    <div class="progress-container">
        <div class="progress-bar-wrapper">
            <div class="progress-bar-fill" style="width: {percent}%;"></div>
        </div>
        <div class="progress-text-row">
            <span class="progress-percent">{percent}%</span>
            <span class="progress-label">{label}</span>
        </div>
    </div>
    """

def render_custom_progress_bar(percent, label):
    st.markdown(get_custom_progress_bar_html(percent, label), unsafe_allow_html=True)

def run_bg_compilation(template_paths, built_urls, output_name, output_path, temp_dir, status_dict, selected_idx):
    status_dict["in_progress"] = True
    status_dict["done"] = False
    status_dict["success"] = False
    status_dict["error"] = None
    status_dict["logs"] = []
    status_dict["percent"] = 5
    status_dict["label"] = "بدء معالجة الفيديوهات... 5%"
    write_progress(5, status_dict["label"])

    def log_compile(msg):
        css_cls = "log-line-ok" if "✅" in msg or "Done" in msg or "ready" in msg else ("log-line-err" if "❌" in msg or "failed" in msg else "log-line-run")
        status_dict["logs"].append(f'<div class="{css_cls}">{msg}</div>')

    try:
        import shutil
        import concurrent.futures
        import time
        import threading
        
        temp_out = os.path.join(temp_dir, f"compiled_{int(time.time())}.mp4")
        
        # Rearrange templates and urls so the selected video is processed first AND placed first in compilation
        ordered_template_paths = [template_paths[selected_idx]] + [
            path for idx, path in enumerate(template_paths) if idx != selected_idx
        ]
        ordered_built_urls = [built_urls[selected_idx]] + [
            url for idx, url in enumerate(built_urls) if idx != selected_idx
        ]
        
        M = len(ordered_template_paths)
        status_dict["percent"] = 20
        status_dict["label"] = "جاري معالجة وتجميع المقاطع إلى الفيديو النهائي... 20%"
        write_progress(20, status_dict["label"])
        log_compile("🔗 جاري معالجة وتجميع المقاطع إلى الفيديو النهائي...")
        compile_from_templates(ordered_template_paths, temp_out, progress_cb=log_compile)
        
        status_dict["percent"] = 95
        status_dict["label"] = "جاري حفظ وتخزين الملف النهائي... 95%"
        write_progress(95, status_dict["label"])
        log_compile(f"✅ الملف جاهز: {temp_out}")
        
        try:
            shutil.copy2(temp_out, output_path)
            log_compile(f"✅ تم الحفظ: {output_path}")
        except Exception as copy_err:
            log_compile(f"⚠️ تعذر النسخ: {copy_err}")

        status_dict["output_path"] = output_path
        status_dict["output_name"] = output_name
        status_dict["success"] = True
        status_dict["percent"] = 100
        status_dict["label"] = "تم تجميع الفيديو بنجاح ✓ 100%"
        write_progress(100, status_dict["label"], done=True)
    except Exception as err:
        log_compile(f"❌ خطأ: {err}")
        status_dict["error"] = str(err)
        status_dict["label"] = f"❌ خطأ في التجميع: {err}"
        write_progress(100, status_dict["label"], done=True)
    finally:
        status_dict["in_progress"] = False
        status_dict["done"] = True

# --- Utilities & Progress Tracking ---
import socket
import re
import struct
import subprocess

def get_local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        pass
    return "127.0.0.1"

def write_progress(percent, label, done=False):
    import json
    import streamlit as st
    try:
        st_static_path = os.path.join(os.path.dirname(st.__file__), "static")
        app_media_dir = os.path.join(st_static_path, "app_media")
        os.makedirs(app_media_dir, exist_ok=True)
        progress_file = os.path.join(app_media_dir, "progress.json")
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump({"percent": percent, "label": label, "done": done}, f)
    except Exception:
        pass

def render_custom_progress_bar_js():
    html_code = f"""
    <div class="js-progress-container" id="progress-container-root">
        <div class="progress-bar-wrapper">
            <div class="progress-bar-fill" id="progress-bar-fill-node" style="width: 5%;"></div>
        </div>
        <div class="progress-text-row">
            <span class="progress-percent" id="progress-percent-node">5%</span>
            <span class="progress-label" id="progress-label-node">جاري بدء التجميع...</span>
        </div>
    </div>
    
    <script>
    (function() {{
        const bar = document.getElementById("progress-bar-fill-node");
        const pct = document.getElementById("progress-percent-node");
        const lbl = document.getElementById("progress-label-node");
        
        let intervalId = setInterval(updateProgress, 1000);
        
        function updateProgress() {{
            fetch("app_media/progress.json?t=" + Date.now())
                .then(r => r.json())
                .then(data => {{
                    if (data && typeof data.percent !== 'undefined') {{
                        bar.style.width = data.percent + "%";
                        pct.innerText = data.percent + "%";
                        lbl.innerText = data.label || "";
                        
                        if (data.done || data.percent >= 100) {{
                            clearInterval(intervalId);
                            bar.style.width = "100%";
                            pct.innerText = "100%";
                            lbl.innerText = "تم تجميع وتعديل الفيديو بنجاح ✓ 100%";
                            
                            setTimeout(() => {{
                                try {{
                                    const doc = window.parent.document;
                                    const buttons = doc.querySelectorAll("button");
                                    for (let btn of buttons) {{
                                        if (btn.textContent && btn.textContent.includes("تحديث الفيديو")) {{
                                            btn.click();
                                            break;
                                        }}
                                    }}
                                }} catch (e) {{
                                    console.error("Error auto-clicking refresh button:", e);
                                }}
                            }}, 500);
                        }}
                    }}
                }})
                .catch(err => {{
                    console.log("Error fetching progress:", err);
                }});
        }}
        // Initial call
        updateProgress();
    }})();
    </script>
    
    <style>
    .js-progress-container {{
        background: rgba(255, 255, 255, 0.02);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 10px;
        padding: 12px 16px;
        margin: 10px 0;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    }}
    .progress-bar-wrapper {{
        background: rgba(255, 255, 255, 0.05);
        border-radius: 4px;
        height: 6px;
        overflow: hidden;
        margin-bottom: 6px;
    }}
    .progress-bar-fill {{
        background: linear-gradient(90deg, #e11d48, #f43f5e);
        height: 100%;
        width: 5%;
        transition: width 0.4s ease-in-out;
    }}
    .progress-text-row {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 0.75rem;
        color: #94a3b8;
        direction: rtl;
    }}
    .progress-percent {{
        font-weight: bold;
        color: #f43f5e;
    }}
    .progress-label {{
        font-weight: 500;
    }}
    </style>
    """
    st.components.v1.html(html_code, height=65)

def extract_waveform_peaks(audio_path: str, num_peaks: int = 500) -> list[float]:
    cmd = [
        "ffmpeg", "-y", "-nostdin",
        "-i", audio_path,
        "-ac", "1",
        "-ar", "1000",
        "-f", "s16le",
        "-"
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL)
        if res.returncode != 0:
            return [0.5] * num_peaks
        pcm_data = res.stdout
        num_samples = len(pcm_data) // 2
        if num_samples == 0:
            return [0.0] * num_peaks
        samples = struct.unpack(f"<{num_samples}h", pcm_data)
        max_val = max(abs(s) for s in samples) if samples else 1.0
        if max_val == 0:
            max_val = 1.0
        normalized = [abs(s) / max_val for s in samples]
        
        peaks = []
        chunk_size = len(normalized) / num_peaks
        for i in range(num_peaks):
            start_idx = int(i * chunk_size)
            end_idx = int((i + 1) * chunk_size)
            chunk = normalized[start_idx:end_idx]
            if chunk:
                peaks.append(round(max(chunk), 3))
            else:
                peaks.append(0.0)
        return peaks
    except Exception:
        return [0.5] * num_peaks

def extract_video_thumbnails(video_path: str, num_thumbs: int = 5):
    temp_dir = os.path.join(os.path.dirname(os.path.abspath(video_path)), "temp_processing")
    os.makedirs(temp_dir, exist_ok=True)
    try:
        dur = get_duration(video_path)
        interval = dur / (num_thumbs + 1)
        for i in range(num_thumbs):
            t = (i + 1) * interval
            thumb_path = os.path.join(temp_dir, f"thumb_{i}.jpg")
            cmd = [
                "ffmpeg", "-y", "-ss", f"{t:.3f}",
                "-i", video_path,
                "-vframes", "1",
                "-vf", "scale=120:70",
                "-f", "image2",
                thumb_path
            ]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    except Exception as e:
        print(f"Error extracting thumbnails: {e}")



def get_base64_encoded_file(path):
    import base64
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        data = f.read()
    return base64.b64encode(data).decode('utf-8')

def get_mime_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp4": return "video/mp4"
    if ext == ".mp3": return "audio/mpeg"
    if ext == ".wav": return "audio/wav"
    if ext == ".m4a": return "audio/x-m4a"
    return "application/octet-stream"


# ─── Helper: word count ───────────────────────────────────────────────────────

def count_words(text: str) -> int:
    """Count words in a French/Arabic text string."""
    import re as _re
    return len(_re.findall(r'\S+', text.strip()))


# ─── Helper: ElevenLabs voices ───────────────────────────────────────────────

def get_elevenlabs_voices(api_key: str) -> list:
    """Fetch available voices from ElevenLabs API."""
    try:
        import requests as _req
        resp = _req.get(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": api_key},
            timeout=20
        )
        if resp.status_code == 200:
            data = resp.json()
            voices = []
            for v in data.get("voices", []):
                voices.append({
                    "voice_id": v.get("voice_id", ""),
                    "name": v.get("name", ""),
                    "preview_url": v.get("preview_url", ""),
                    "category": v.get("category", ""),
                })
        return []
    except Exception:
        return []


# ─── Helper: Audio Compression & Groq Whisper ──────────────────────────────────

def compress_audio_for_upload(input_path: str) -> str:
    """Compress audio to a lightweight mono MP3 at 16kHz 32kbps to speed up upload."""
    import tempfile
    import subprocess
    import time
    
    suffix = ".mp3"
    temp_dir = os.path.dirname(input_path)
    compressed_path = os.path.join(temp_dir, f"compressed_{int(time.time())}{suffix}")
    
    cmd = [
        "ffmpeg", "-y", "-nostdin",
        "-i", input_path,
        "-ac", "1",
        "-ar", "16000",
        "-b:a", "32k",
        compressed_path
    ]
    try:
        # Run silently
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, check=True)
        if os.path.exists(compressed_path):
            return compressed_path
    except Exception:
        # Fallback to original path if compression fails
        pass
    return input_path

def transcribe_audio_with_groq(audio_path: str, api_key: str) -> tuple[str, list]:
    """
    Transcribe audio file using Groq's Whisper API.
    Returns (transcription_text, word_alignments_list).
    """
    import requests
    import json
    import time
    
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {api_key}"
    }
    
    # Compress the audio first to make it super fast to upload
    compressed_path = compress_audio_for_upload(audio_path)
    
    try:
        with open(compressed_path, "rb") as f:
            files = {
                "file": (os.path.basename(compressed_path), f, "audio/mpeg")
            }
            data = {
                "model": "whisper-large-v3",
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word"
            }
            
            resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)
            
        # Cleanup compressed temp file if it's different from the original
        if compressed_path != audio_path and os.path.exists(compressed_path):
            try:
                os.remove(compressed_path)
            except Exception:
                pass
                
        if resp.status_code == 200:
            resp_data = resp.json()
            text = resp_data.get("text", "")
            
            # Extract word alignments
            words = []
            raw_words = resp_data.get("words", [])
            
            # If not at top level, check segments
            if not raw_words and "segments" in resp_data:
                for seg in resp_data["segments"]:
                    if "words" in seg:
                        raw_words.extend(seg["words"])
            
            for w in raw_words:
                word_text = w.get("word", "").strip()
                if word_text:
                    words.append({
                        "text": word_text,
                        "start": w.get("start", 0.0),
                        "end": w.get("end", 0.0)
                    })
            
            return text, words
        else:
            err_msg = resp.text
            try:
                err_data = resp.json()
                if "error" in err_data:
                    err_msg = err_data["error"].get("message", resp.text)
            except Exception:
                pass
            raise RuntimeError(f"Groq API Error (Status {resp.status_code}): {err_msg}")
            
    except Exception as e:
        # Cleanup compressed temp file in case of exception
        if compressed_path != audio_path and os.path.exists(compressed_path):
            try:
                os.remove(compressed_path)
            except Exception:
                pass
        raise e


# ─── Helper: ElevenLabs TTS ──────────────────────────────────────────────────

def generate_elevenlabs_audio(text: str, voice_id: str, api_key: str, output_path: str, with_timestamps: bool = False) -> tuple[bool, str, dict | None]:
    """Generate voiceover audio from ElevenLabs API and save to output_path."""
    try:
        import requests as _req
        import base64
        
        if with_timestamps:
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
        else:
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json" if with_timestamps else "audio/mpeg",
        }
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.80,
                "style": 0.20,
                "use_speaker_boost": True,
            },
        }
        resp = _req.post(url, json=payload, headers=headers, timeout=180)
        if resp.status_code == 200:
            if with_timestamps:
                try:
                    data = resp.json()
                    audio_b64 = data.get("audio_base64", "")
                    alignment = data.get("alignment", None)
                    if not audio_b64:
                        return False, "ElevenLabs response did not contain audio_base64", None
                    audio_bytes = base64.b64decode(audio_b64)
                    with open(output_path, "wb") as f:
                        f.write(audio_bytes)
                    return True, "", alignment
                except Exception as json_err:
                    return False, f"Failed to parse alignment JSON: {str(json_err)}", None
            else:
                with open(output_path, "wb") as f:
                    f.write(resp.content)
                return True, "", None
        
        # Try to parse error details
        err_msg = ""
        try:
            err_data = resp.json()
            if isinstance(err_data, dict):
                # ElevenLabs often returns error in {"detail": {"message": ...}} or {"detail": "..."} or {"message": "..."}
                detail = err_data.get("detail", {})
                if isinstance(detail, dict):
                    err_msg = detail.get("message", str(err_data))
                else:
                    err_msg = str(detail) or err_data.get("message", str(err_data))
            else:
                err_msg = str(err_data)
        except Exception:
            err_msg = resp.text or f"Status code: {resp.status_code}"
            
        return False, f"ElevenLabs Error (Status {resp.status_code}): {err_msg}", None
    except Exception as e:
        return False, f"Exception occurred: {str(e)}", None


def parse_elevenlabs_alignment(alignment: dict) -> list:
    """Group character alignments from ElevenLabs into word-level alignments."""
    if not alignment:
        return []
    characters = alignment.get("characters", [])
    start_times = alignment.get("character_start_times_seconds", [])
    end_times = alignment.get("character_end_times_seconds", [])
    
    if not characters or not start_times or not end_times:
        return []
        
    words = []
    current_word_chars = []
    current_word_start = None
    current_word_end = None
    
    for char, start, end in zip(characters, start_times, end_times):
        if char.isspace():
            if current_word_chars:
                word_text = "".join(current_word_chars)
                words.append({
                    "text": word_text,
                    "start": current_word_start,
                    "end": current_word_end
                })
                current_word_chars = []
                current_word_start = None
                current_word_end = None
        else:
            if not current_word_chars:
                current_word_start = start
            current_word_chars.append(char)
            current_word_end = end
            
    if current_word_chars:
        word_text = "".join(current_word_chars)
        words.append({
            "text": word_text,
            "start": current_word_start,
            "end": current_word_end
        })
        
    return words


def group_words(words: list, max_words: int = 3, max_duration: float = 1.8, max_chars: int = 25) -> list:
    """Group words into subtitle lines based on word count, duration, and length constraints."""
    groups = []
    current_group = []
    
    for word in words:
        if not current_group:
            current_group.append(word)
            continue
            
        group_duration = word["end"] - current_group[0]["start"]
        group_words_count = len(current_group) + 1
        group_chars_count = sum(len(w["text"]) for w in current_group) + len(word["text"]) + len(current_group)
        
        gap = word["start"] - current_group[-1]["end"]
        
        if (group_words_count <= max_words and 
            group_duration <= max_duration and 
            group_chars_count <= max_chars and 
            gap < 0.4):
            current_group.append(word)
        else:
            groups.append(current_group)
            current_group = [word]
            
    if current_group:
        groups.append(current_group)
        
    return groups


def format_ass_time(seconds: float) -> str:
    """Format seconds into ASS format (H:MM:SS.cs)."""
    if seconds < 0:
        seconds = 0.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centiseconds = int(round((seconds - int(seconds)) * 100))
    if centiseconds == 100:
        secs += 1
        centiseconds = 0
        if secs == 60:
            minutes += 1
            secs = 0
            if minutes == 60:
                hours += 1
                minutes = 0
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def generate_ass_file(words: list, subtitle_mode: str) -> str | None:
    """Generate the contents of an ASS subtitle file using word alignments."""
    if not words or subtitle_mode not in ["green", "blue"]:
        return None
        
    highlight_tag = "{\\c&H00FF00&}" if subtitle_mode == "green" else "{\\c&HFF9900&}"
    white_tag = "{\\c&HFFFFFF&}"
    
    # Check if the text contains Arabic characters to avoid font fallback issues in libass
    has_arabic = any(any(u"\u0600" <= char <= u"\u06FF" for char in w["text"]) for w in words)
    fontname = "Cairo" if has_arabic else "Impact"
    
    groups = group_words(words)
    
    # Fontname: Arial Black (for Arabic) / Impact (for French), Fontsize: 75, Outline: 3.5, Shadow: 1.5
    # PlayResX: 1080, PlayResY: 1920, MarginV: 770
    ass_lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{fontname},75,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3.5,1.5,2,80,80,770,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]
    
    for group in groups:
        if not group:
            continue
            
        for idx, w_active in enumerate(group):
            w_start = w_active["start"]
            w_end = w_active["end"]
            
            if w_end <= w_start:
                w_end = w_start + 0.1
                
            start_str = format_ass_time(w_start)
            end_str = format_ass_time(w_end)
            
            parts = []
            for w in group:
                word_text = w['text']
                if not has_arabic:
                    word_text = word_text.upper()  # UPPERCASE for professional CapCut viral caption look (French only)
                if w == w_active:
                    parts.append(f"{highlight_tag}{word_text}")
                else:
                    parts.append(f"{white_tag}{word_text}")
            text_str = " ".join(parts)
            
            ass_lines.append(f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{text_str}")
            
            # If there is a gap to the next word in the group, show all white
            if idx < len(group) - 1:
                w_next = group[idx + 1]
                gap_start = w_end
                gap_end = w_next["start"]
                if gap_end - gap_start > 0.05:
                    gap_start_str = format_ass_time(gap_start)
                    gap_end_str = format_ass_time(gap_end)
                    parts_white = []
                    for w in group:
                        word_text = w['text']
                        if not has_arabic:
                            word_text = word_text.upper()
                        parts_white.append(f"{white_tag}{word_text}")
                    text_white = " ".join(parts_white)
                    ass_lines.append(f"Dialogue: 0,{gap_start_str},{gap_end_str},Default,,0,0,0,,{text_white}")
                    
    return "\n".join(ass_lines)


# ─── Helper: TurnScribe transcription ────────────────────────────────────────

def get_turnscribe_transcription(video_url: str, api_key: str) -> str:
    """
    Request transcription from TurnScribe using their web client endpoint.
    This bypasses the broken/defunct app.turnscribe.com subdomain and works reliably with standard browser headers.
    """
    try:
        import requests as _req, time as _time
        session = _req.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://turnscribeai.com/"
        })
        
        # 1. Fetch homepage to get CSRF token
        home_resp = session.get("https://turnscribeai.com/", timeout=15)
        if home_resp.status_code != 200:
            return f"Error: Failed to load TurnScribe homepage (Status {home_resp.status_code})"
            
        csrf_token = session.cookies.get("csrftoken")
        if not csrf_token:
            return "Error: Could not retrieve CSRF token from TurnScribe."
            
        # 2. Submit transcription job to /process/
        submit_resp = session.post(
            "https://turnscribeai.com/process/",
            headers={"X-CSRFToken": csrf_token},
            data={"url": video_url},
            timeout=20
        )
        if submit_resp.status_code != 200:
            return f"Error: Submission failed (Status {submit_resp.status_code}): {submit_resp.text[:200]}"
            
        res_data = submit_resp.json()
        if res_data.get("error"):
            return f"Error: {res_data.get('error')}"
            
        # If the video was already transcribed (cached), return the content directly
        if res_data.get("reused") or res_data.get("status") == "COMPLETED":
            content = res_data.get("content") or res_data.get("text")
            if content:
                return str(content)
                
        task_id = res_data.get("task_id")
        trans_id = res_data.get("trans_id")
        if not task_id or not trans_id:
            return f"Error: Unexpected response format: {str(res_data)[:200]}"
            
        # 3. Poll /status/ for the result
        for _ in range(60):
            _time.sleep(3)
            status_resp = session.get(
                f"https://turnscribeai.com/status/?task_id={task_id}&trans_id={trans_id}",
                timeout=15
            )
            if status_resp.status_code != 200:
                continue
            
            status_data = status_resp.json()
            status = status_data.get("status", "")
            if status == "SUCCESS":
                content = status_data.get("content") or status_data.get("text") or status_data.get("transcription", "")
                return str(content)
            elif status in ("FAILURE", "ERROR", "failed", "error"):
                return f"Error: Transcription failed: {status_data.get('error', 'unknown error')}"
                
        return "Error: Transcription timed out after 3 minutes."
    except Exception as e:
        return f"Error: {e}"


# ─── Callbacks for API Keys settings ───────────────────────────────────────

def clear_api_keys():
    st.session_state.user_openrouter_key = ""
    st.session_state.user_elevenlabs_key = ""
    st.session_state.user_groq_key = ""
    st.session_state.widget_api_key_input = ""
    st.toast("تم مسح المفاتيح المخصصة بنجاح 🗑️")





# ─── Helper: OpenRouter script optimization ──────────────────────────────────

def run_openrouter_optimization(script: str, custom_api_key: str = "") -> str:
    """
    Send script to OpenRouter for optimization using Claude models.
    Requires a valid OpenRouter API key provided by the user.
    """
    try:
        import requests as _req
        api_key = custom_api_key.strip()
        if not api_key:
            return "Error: ⚠️ لا يوجد مفتاح OpenRouter API. أضف مفتاحك في خانة إعدادات مفاتيح API أعلاه (يبدأ بـ sk-or-)."

        system_prompt = (
            "Tu es un expert en création de scripts vidéo viraux pour TikTok en français.\n\n"

            "MISSION:\n"
            "1. Si le texte n'est pas en français, traduis-le d'abord en français.\n"
            "2. Applique une réécriture fluide et engageante sur TOUT le texte.\n"
            "3. Ensuite, applique cette stratégie de découpage en DEUX PARTIES:\n\n"

            "   PARTIE 1 — Les PREMIERS 100 mots environ:\n"
            "   → Garde-les presque intacts. Réécriture LÉGÈRE seulement pour améliorer le rythme.\n"
            "   → Ne supprime pas d'informations importantes.\n\n"

            "   PARTIE 2 — Le RESTE du texte (tout ce qui dépasse les 100 premiers mots):\n"
            "   → Condense et résume jusqu'à obtenir environ 80 à 85 mots MAXIMUM.\n"
            "   → Garde les informations essentielles, supprime les détails secondaires.\n\n"

            "RÈGLE ABSOLUE — JAMAIS de phrase coupée:\n"
            "   → Chaque phrase DOIT être complète. Ne coupe JAMAIS une phrase au milieu.\n"
            "   ✅ Correct : '...et quand il rentra chez lui, il la trouva là.'\n"
            "   ❌ INTERDIT : '...et quand il rentra chez'\n"
            "   → Si tu approches de la limite de mots, TERMINE la phrase en cours avant d'arrêter.\n\n"

            "FORMAT DE RÉPONSE REQUIS (STRICT):\n"
            "   → Le script doit être UNIQUEMENT le texte parlé, fluide et propre.\n"
            "   → Interdiction ABSOLUE d'inclure des titres de sections (ex: 'Partie 1', 'Partie 2', 'Intro', 'Outro').\n"
            "   → Interdiction ABSOLUE d'inclure des indications de mots (ex: '(100 mots)', '(85 mots)', '(185 mots)').\n"
            "   → Interdiction ABSOLUE d'écrire des phrases d'introduction ou de conclusion (ex: 'Voici un script...', 'Commençons. Commençons.').\n"
            "   → Ta réponse doit commencer directement par la première phrase parlée du script, et se terminer par la dernière phrase du script.\n\n"

            "CONTRAINTE FINALE:\n"
            "   → Le script total DOIT être entre 177 et 185 mots. Compte mot par mot avant de répondre.\n"
            "   → Réponds UNIQUEMENT avec le script final en français. Aucune explication, aucun commentaire, aucun titre."
        )

        # Claude models first, then free fallbacks if credit runs out
        models_to_try = [
            "anthropic/claude-sonnet-4-5",
            "anthropic/claude-3.5-sonnet",
            "anthropic/claude-3-haiku",
            "google/gemini-2.5-flash:free",
            "openrouter/free",
        ]

        def _count_words(text: str) -> int:
            import re as _re
            return len(_re.findall(r'\S+', text.strip()))

        def _clean_script(text: str) -> str:
            import re as _re
            if not text:
                return ""
            # 1. Remove bracketed word counts like (185 mots), (100 mots), (85 words), etc.
            text = _re.sub(r'\(\d+\s*mots?\)', '', text, flags=_re.IGNORECASE)
            text = _re.sub(r'\(\d+\s*words?\)', '', text, flags=_re.IGNORECASE)
            
            # 2. Split into lines to clean line by line
            lines = text.split('\n')
            cleaned_lines = []
            
            for line in lines:
                s_line = line.strip()
                if not s_line:
                    continue
                    
                # Skip conversational/header lines like "Voici un script...", "Voici le script...", "Voici une version..."
                if _re.match(r'^(voici|voici\s+un\s+script|voici\s+une\s+version|voici\s+le\s+script|ce\s+script|script\s+vidéo)', s_line, _re.IGNORECASE):
                    # If it's just a short header ending with colon or period, skip it
                    if len(s_line) < 100 and (s_line.endswith(':') or s_line.endswith('.')):
                        continue
                        
                # Skip standalone repetitive greeting words at the very beginning if they resemble headers
                if s_line.lower() in ["commençons. commençons.", "commençons", "commençons !"]:
                    if len(cleaned_lines) == 0:
                        continue

                # Skip section markers like "Partie 1 :", "Partie 2 :", "Intro:", "Hook:", "Outro:"
                pattern_section = r'^(partie\s*\d+|intro|outro|hook|body|conclusion|introduction|transition)\b'
                if _re.match(pattern_section, s_line, _re.IGNORECASE):
                    if len(s_line) < 60:
                        continue
                        
                cleaned_lines.append(line)
                
            result = '\n'.join(cleaned_lines).strip()
            result = _re.sub(r'\n+', '\n', result)
            return result

        def _call_claude(model: str, messages: list, max_tokens: int = 400) -> str | None:
            resp = _req.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://appk-mobil.streamlit.app",
                    "X-Title": "Appk Mobil Script Optimizer",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.7,
                },
                timeout=45
            )
            if resp.status_code in (401, 403):
                raise PermissionError("invalid_key")
            
            # Extract error details instead of returning None silently
            err_msg = ""
            try:
                err_data = resp.json()
                if "error" in err_data:
                    err_msg = err_data["error"].get("message", str(err_data["error"]))
                else:
                    err_msg = resp.text
            except Exception:
                err_msg = resp.text or f"Status code: {resp.status_code}"
                
            if resp.status_code == 402:
                # Attempt to extract affordable tokens from error message
                import re as _re
                match = _re.search(r"can only afford (\d+)", err_msg)
                if match:
                    affordable = int(match.group(1))
                    # If they can afford at least 150 tokens, try to retry with that amount
                    if affordable >= 150 and max_tokens > affordable:
                        return _call_claude(model, messages, max_tokens=affordable)
                raise RuntimeError(f"OpenRouter Error (Status 402): {err_msg}")
                
            if resp.status_code == 200:
                data = resp.json()
                try:
                    content = data["choices"][0]["message"].get("content")
                    if content:
                        return content.strip()
                except Exception:
                    pass
                return None
                
            raise RuntimeError(f"OpenRouter Error (Status {resp.status_code}): {err_msg}")

        last_error = ""
        for model in models_to_try:
            try:
                # ─── Appel 1: optimisation initiale ───────────────────────
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": f"Traduis et optimise ce script en 177-185 mots exactement (cible: 180 mots):\n\n{script}"}
                ]
                content = _call_claude(model, messages)
                if not content:
                    continue

                content = _clean_script(content)
                wc = _count_words(content)

                # ─── Appel 2: correction si hors plage ────────────────────
                if not (177 <= wc <= 185):
                    if wc > 185:
                        correction_msg = (
                            f"Ton script contient {wc} mots — c'est trop long. "
                            f"Tu DOIS le raccourcir pour obtenir EXACTEMENT entre 177 et 185 mots. "
                            f"Supprime des mots/phrases dans la DEUXIÈME PARTIE du texte uniquement. "
                            f"RÈGLE ABSOLUE: ne coupe JAMAIS une phrase au milieu — chaque phrase doit être complète. "
                            f"Réponds UNIQUEMENT avec le script corrigé, sans en-tête ni commentaire ni titre.\n\n"
                            f"Script à corriger:\n{content}"
                        )
                    else:
                        correction_msg = (
                            f"Ton script contient {wc} mots — c'est trop court. "
                            f"Tu DOIS l'allonger pour obtenir EXACTEMENT entre 177 et 185 mots. "
                            f"Ajoute des détails dans la DEUXIÈME PARTIE du texte. "
                            f"Réponds UNIQUEMENT avec le script corrigé, sans en-tête ni commentaire ni titre.\n\n"
                            f"Script à corriger:\n{content}"
                        )
                    messages_v2 = [
                        {"role": "system",    "content": system_prompt},
                        {"role": "user",      "content": f"Traduis et optimise ce script en 177-185 mots exactement:\n\n{script}"},
                        {"role": "assistant", "content": content},
                        {"role": "user",      "content": correction_msg},
                    ]
                    content_v2 = _call_claude(model, messages_v2)
                    if content_v2:
                        content_v2 = _clean_script(content_v2)
                        wc2 = _count_words(content_v2)
                        # Accepter si plus proche de la plage
                        if 177 <= wc2 <= 185:
                            return content_v2
                        # Sinon garder le plus proche
                        if abs(wc2 - 181) < abs(wc - 181):
                            content, wc = content_v2, wc2

                if content:
                    return content

            except PermissionError:
                return "Error: ❌ مفتاح OpenRouter غير صالح أو منتهي الصلاحية. تحقق من مفتاحك."
            except Exception as model_err:
                last_error = f"{model} failed: {model_err}"

        return f"Error: {last_error}"
    except Exception as e:
        return f"Error: {e}"


st.set_page_config(
    page_title="Video Processing Studio",
    page_icon="🎬",
    layout="centered",
    initial_sidebar_state="collapsed",
)

if "compilation_done" not in st.session_state:
    st.session_state.compilation_done = False
if "output_path" not in st.session_state or not st.session_state.output_path:
    st.session_state.output_path = os.path.abspath("compiled_output.mp4")
if "output_name" not in st.session_state or not st.session_state.output_name:
    st.session_state.output_name = "compiled_output.mp4"
if "templates_built" not in st.session_state:
    st.session_state.templates_built = False
if "template_paths" not in st.session_state:
    st.session_state.template_paths = []
if "built_urls" not in st.session_state:
    st.session_state.built_urls = []
if "trim_start" not in st.session_state:
    st.session_state.trim_start = 0.0
if "trim_end" not in st.session_state:
    st.session_state.trim_end = 61.0
if "delay_sec" not in st.session_state:
    st.session_state.delay_sec = 0.0
if "volume" not in st.session_state:
    st.session_state.volume = 1.0
if "auto_mix" not in st.session_state:
    st.session_state.auto_mix = False
if "clips" not in st.session_state:
    st.session_state.clips = []
# ─── Load API keys from disk (persisted across restarts) ───────────────────────
if "keys_loaded" not in st.session_state:
    _saved_keys = _load_saved_api_keys()
    st.session_state.user_openrouter_key = _saved_keys.get("openrouter", "")
    st.session_state.user_elevenlabs_key = _saved_keys.get("elevenlabs", "")
    st.session_state.user_groq_key = _saved_keys.get("groq", "")
    st.session_state.keys_loaded = True

# script textarea version counter (increments on extraction/optimize to force fresh render)
if "script_area_version" not in st.session_state:
    st.session_state.script_area_version = 0


# --- Query Params Handler ---
import json
import base64
params = st.query_params
if "mix_audio" in params:
    try:
        clips_str = params.get("clips", "[]")
        st.session_state.clips = json.loads(clips_str)
        st.session_state.auto_mix = True
    except Exception:
        pass
    st.query_params.clear()



def get_image_base64_css(path):
    import os
    import base64
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "rb") as image_file:
            encoded = base64.b64encode(image_file.read()).decode()
        return f"background-image: url('data:image/png;base64,{encoded}') !important;"
    except Exception:
        return ""

bg_img_css = ""


st.markdown("""
<!-- Load Tailwind CSS -->
<script src="https://cdn.tailwindcss.com"></script>
<!-- Load GSAP -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.2/gsap.min.js"></script>

<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Outfit:wght@700;800;900&display=swap');

/* ─── VARIABLES ─── */
:root {
  --accent:        #8b1a1a;
  --accent-dark:   #6b1212;
  --accent-deeper: #4a0d0d;
  --accent-glow:   rgba(139, 26, 26, 0.45);
  --accent-soft:   rgba(139, 26, 26, 0.12);
  --accent-border: rgba(139, 26, 26, 0.65);
  --bg:            #0a0a0a;
  --card-bg:       #111111;
  --inner-bg:      #181818;
  --border:        rgba(255,255,255,0.06);
  --text:          #e2e8f0;
  --muted:         #666666;
}

/* ─── BASE ─── */
*, *::before, *::after { box-sizing: border-box; }

html, body {
    overflow: hidden !important;
    height: 100vh !important;
    width: 100vw !important;
    margin: 0 !important;
    padding: 0 !important;
    background: transparent !important;
    background-color: transparent !important;
    font-family: 'Inter', sans-serif !important;
}

#root, 
.stApp, 
[data-testid="stAppViewContainer"], 
.main, 
section.main, 
.block-container, 
[data-testid="block-container"] {
    background-color: transparent !important;
    background-image: none !important;
}

/* ─── DRAGON BACKGROUND ─── */
.dragon-background-container {
    position: fixed;
    top: 0;
    left: 0;
    width: 100vw;
    height: 100vh;
    z-index: -1 !important; /* sit behind everything */
    overflow: hidden;
    pointer-events: none;
    display: flex;
    justify-content: center;
    align-items: center;
}
.dragon-svg {
    width: 60%;
    height: 60%;
    max-width: 650px;
    max-height: 650px;
    opacity: 0.18; /* majestic, subtle silver background */
    transform-origin: 50% 50%;
    animation: dragonFly 12s ease-in-out infinite;
    pointer-events: none;
}
#dragon-fire-canvas {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    z-index: 1;
}

@keyframes dragonFly {
    0% {
        transform: translateY(-8px) rotate(-0.5deg) scale(1);
    }
    50% {
        transform: translateY(12px) rotate(0.5deg) scale(0.97, 1.02) skewX(0.5deg);
    }
    100% {
        transform: translateY(-8px) rotate(-0.5deg) scale(1);
    }
}

/* Hide Streamlit chrome */
#MainMenu, header, footer, [data-testid="stHeader"],
[data-testid="stToolbar"], [data-testid="stDecoration"] {
    display: none !important;
    visibility: hidden !important;
    height: 0 !important;
}

/* Center the block-container in viewport */
.main > div {
    display: flex !important;
    justify-content: center !important;
    align-items: center !important;
    height: 100vh !important;
    width: 100vw !important;
    overflow: hidden !important;
    padding: 0 !important;
}

/* ─── MAIN APP WINDOW ─── */
[data-testid="block-container"] {
    width: 420px !important;
    max-width: 96vw !important;
    height: 640px !important;
    max-height: 94vh !important;
    padding: 24px 24px 20px !important;
    margin: 0 !important;
    background: var(--card-bg) !important;
    border: 1px solid var(--border) !important;
    border-radius: 22px !important;
    box-shadow: 0 28px 80px rgba(0,0,0,0.95) !important;
    color: var(--text) !important;
    overflow-y: auto !important;
    box-sizing: border-box !important;
    display: flex !important;
    flex-direction: column !important;
}

[data-testid="block-container"]::-webkit-scrollbar,
[data-testid="stVerticalBlock"]::-webkit-scrollbar {
    width: 5px !important;
}
[data-testid="block-container"]::-webkit-scrollbar-thumb,
[data-testid="stVerticalBlock"]::-webkit-scrollbar-thumb {
    background: rgba(255, 255, 255, 0.12) !important;
    border-radius: 10px !important;
}
[data-testid="block-container"]::-webkit-scrollbar-thumb:hover,
[data-testid="stVerticalBlock"]::-webkit-scrollbar-thumb:hover {
    background: var(--accent) !important;
}

[data-testid="block-container"] > div,
[data-testid="stVerticalBlock"] {
    overflow-y: auto !important;
    flex: 1 !important;
    min-height: 0 !important;
    gap: 0 !important;
}
[data-testid="stVerticalBlock"] > div {
    padding-top: 0 !important;
    padding-bottom: 0 !important;
    margin-top: 0 !important;
    margin-bottom: 0 !important;
}

/* ─── STEP CIRCLES ─── */
.steps-container {
    display: flex !important;
    justify-content: center !important;
    align-items: center !important;
    gap: 8px !important;
    margin-bottom: 4px !important;
    width: 100% !important;
}

.step-circle {
    width: 30px !important;
    height: 30px !important;
    border-radius: 50% !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    font-size: 0.76rem !important;
    font-weight: 700 !important;
    font-family: 'Inter', sans-serif !important;
    transition: all 0.3s ease !important;
    user-select: none !important;
}

.step-circle.inactive {
    background: #1c1c1c !important;
    border: 1.5px solid #2a2a2a !important;
    color: #484848 !important;
}

.step-circle.done {
    background: linear-gradient(135deg, var(--accent) 0%, var(--accent-dark) 100%) !important;
    border: none !important;
    color: #fff !important;
    box-shadow: 0 0 10px var(--accent-glow) !important;
}

.step-circle.active {
    background: linear-gradient(135deg, var(--accent) 0%, var(--accent-dark) 100%) !important;
    border: none !important;
    color: #fff !important;
    box-shadow: 0 0 14px var(--accent-glow) !important;
    transform: scale(1.08) !important;
}

/* Step subtitle */
.step-subtitle {
    text-align: center !important;
    color: var(--muted) !important;
    font-size: 0.7rem !important;
    font-weight: 400 !important;
    margin-bottom: 10px !important;
    font-family: 'Inter', sans-serif !important;
    letter-spacing: 0.2px !important;
}

/* ─── PAGE TITLE (Stage 1) ─── */
.stage1-title {
    font-family: 'Outfit', sans-serif !important;
    font-size: 1.4rem !important;
    font-weight: 800 !important;
    text-align: center !important;
    color: #ffffff !important;
    margin-bottom: 12px !important;
    letter-spacing: -0.6px !important;
}

/* ─── INNER CARD ─── */
.stage1-card {
    background: var(--inner-bg) !important;
    border: 1px solid #242424 !important;
    border-radius: 16px !important;
    padding: 18px 18px 14px !important;
    margin-bottom: 14px !important;
}

/* Default text inputs */
div[data-testid="stTextInput"] {
    margin-bottom: 0 !important;
    margin-top: 0 !important;
}
div[data-testid="stTextInput"] > div {
    margin: 0 !important; padding: 0 !important;
}
div[data-testid="stTextInput"] input {
    background: #0d0d0d !important;
    border: 1px solid #252525 !important;
    border-radius: 10px !important;
    padding: 8px 14px !important;
    color: #cccccc !important;
    font-size: 0.78rem !important;
    height: 46px !important;
    font-family: 'Inter', sans-serif !important;
    transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
}
div[data-testid="stTextInput"] input:focus {
    border-color: var(--accent-border) !important;
    box-shadow: 0 0 0 3px var(--accent-soft) !important;
    background: #0d0d0d !important;
    outline: none !important;
}
div[data-testid="stTextInput"] label {
    display: none !important;
}

/* Sliders */
div[data-testid="stSlider"] {
    margin-bottom: 0 !important;
    margin-top: 0 !important;
    padding: 0 !important;
}
div[data-testid="stSlider"] [data-testid="stThumbValue"] { color: #fff !important; }
div[data-testid="stSlider"] div[role="slider"] {
    background-color: var(--accent) !important;
    border: 2px solid var(--accent) !important;
    box-shadow: none !important;
}
div[data-testid="stSlider"] div[data-track="true"] > div {
    background-color: var(--accent) !important;
}

/* ─── BUTTONS ─── */
div.stButton, div.stDownloadButton {
    margin: 4px 0 !important;
}

/* Default button — ghost / dashed add-link style */
div.stButton > button, div.stDownloadButton > button {
    background: transparent !important;
    color: var(--accent) !important;
    border-radius: 8px !important;
    padding: 8px 16px !important;
    font-size: 0.8rem !important;
    font-weight: 600 !important;
    border: 1.5px dashed var(--accent-border) !important;
    width: 100% !important;
    height: 38px !important;
    box-shadow: none !important;
    transition: background 0.2s ease, color 0.2s ease, border-color 0.2s ease !important;
    display: inline-flex !important;
    justify-content: center !important;
    align-items: center !important;
    cursor: pointer !important;
    font-family: 'Inter', sans-serif !important;
}
div.stButton > button:hover {
    background: var(--accent-soft) !important;
    color: var(--accent) !important;
    border-color: var(--accent) !important;
}
div.stButton > div { padding: 0 !important; margin: 0 !important; }

/* ─── GENERATE BUTTON (last button = primary action) ─── */
div.stButton:last-of-type > button {
    background: linear-gradient(135deg, var(--accent) 0%, var(--accent-dark) 100%) !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 20px !important;
    font-size: 0.82rem !important;
    font-weight: 700 !important;
    height: 40px !important;
    box-shadow: 0 4px 18px var(--accent-glow) !important;
    letter-spacing: 0.3px !important;
    transition: background 0.2s ease, box-shadow 0.2s ease, transform 0.15s ease !important;
}
div.stButton:last-of-type > button:hover {
    background: linear-gradient(135deg, #9e2020 0%, #7a1515 100%) !important;
    box-shadow: 0 6px 22px var(--accent-glow) !important;
    transform: translateY(-1px) !important;
}
div.stButton:last-of-type > button:active {
    transform: translateY(0px) !important;
    box-shadow: 0 2px 10px var(--accent-glow) !important;
}

h3 {
    font-family: 'Outfit', sans-serif !important;
    font-weight: 700 !important;
    font-size: 0.85rem !important;
    color: #ffffff !important;
    margin-top: 10px !important;
    margin-bottom: 6px !important;
}

/* Video players */
video {
    max-height: 180px !important;
    width: 100% !important;
    object-fit: contain !important;
    border-radius: 12px !important;
    background: #000 !important;
    border: 1px solid rgba(255,255,255,0.08) !important;
    box-shadow: 0 10px 25px rgba(0,0,0,0.5) !important;
}

/* File uploader */
[data-testid="stFileUploader"] {
    background-color: #0d0d0d !important;
    border: 1px dashed #2a2a2a !important;
    border-radius: 12px !important;
    padding: 8px !important;
}

/* Notification / alerts */
div[data-testid="stNotification"] {
    background: rgba(255,255,255,0.02) !important;
    border: 1px solid rgba(255,255,255,0.1) !important;
    border-radius: 12px !important;
    backdrop-filter: blur(10px) !important;
    color: var(--text) !important;
}

/* Log box */
.log-box {
    background: rgba(0,0,0,0.4);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 10px;
    padding: 8px;
    font-size: 0.65rem;
    color: #a1a1aa;
    max-height: 80px;
    overflow-y: auto;
}

/* Slide-in animation for new rows */
@keyframes slideIn {
    from { opacity: 0; transform: translateY(-8px); }
    to   { opacity: 1; transform: translateY(0);    }
}
.url-input-row.new { animation: slideIn 0.25s ease forwards; }

/* Style the inputs inside the card to look clean */
.url-input-row {
    display: flex !important;
    align-items: center !important;
    background: #0d0d0d !important;
    border: 1px solid #252525 !important;
    border-radius: 10px !important;
    margin-bottom: 8px !important;
    padding: 0 14px !important;
    height: 42px !important;
    transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
    gap: 10px !important;
    width: 100% !important;
}
.url-input-row:focus-within {
    border-color: var(--accent-border) !important;
    box-shadow: 0 0 0 3px var(--accent-soft) !important;
}
.url-input-row div[data-testid="stTextInput"] {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    margin: 0 !important;
    padding: 0 !important;
    flex: 1 !important;
    width: 100% !important;
}
.url-input-row div[data-testid="stTextInput"] > div {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    margin: 0 !important;
    padding: 0 !important;
}
.url-input-row div[data-testid="stTextInput"] input {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    color: #bbb !important;
    font-size: 0.78rem !important;
    height: 40px !important;
    padding: 0 !important;
    margin: 0 !important;
    outline: none !important;
}
.url-chain-icon {
    font-size: 0.9rem !important;
    color: var(--accent) !important;
    user-select: none !important;
}

/* Max note */
#max-note {
    text-align: center;
    font-size: 0.7rem;
    color: var(--accent);
    margin-top: 8px;
    opacity: 0.7;
}

/* Badge styles */
.badge-cached {
    background: var(--accent-soft) !important;
    color: var(--accent) !important;
    padding: 2px 8px !important;
    border-radius: 20px !important;
    font-size: 0.62rem !important;
    font-weight: 700;
}
.badge-new {
    background: rgba(255,255,255,0.1) !important;
    color: #aaaaaa !important;
    padding: 2px 8px !important;
    border-radius: 20px !important;
    font-size: 0.62rem !important;
    font-weight: 600;
}

/* Mockup preview cards */
.mockup-preview-card {
    display: flex;
    background: #0d0d0d;
    border: 1px solid #242424;
    border-radius: 14px;
    padding: 8px 12px;
    gap: 14px;
    margin-bottom: 10px;
    align-items: center;
}
.mockup-thumb-wrapper {
    position: relative;
    width: 80px; height: 48px;
    border-radius: 8px;
    overflow: hidden;
    border: 1px solid #2a2a2a;
}
.mockup-thumb-img { width: 100%; height: 100%; object-fit: cover; }
.mockup-play-btn {
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    width: 20px; height: 20px;
    background: #fff;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    color: #000; font-size: 0.6rem; font-weight: bold;
}
.mockup-stats {
    display: flex; flex-direction: column; gap: 3px;
    color: #888; font-size: 0.72rem;
}
.mockup-stat-item { display: flex; align-items: center; gap: 6px; }

.hero-title {
    font-family: 'Outfit', sans-serif;
    font-size: 1.1rem; font-weight: 700;
    text-align: center; color: #ffffff;
    margin-top: 5px; margin-bottom: 2px;
}
.hero-sub {
    text-align: center; color: #666;
    font-size: 0.68rem; margin-bottom: 14px;
    text-transform: uppercase; letter-spacing: 0.5px;
}
.card-icons-row { display: flex; justify-content: center; gap: 10px; margin-bottom: 12px; }
.card-icon-circle {
    width: 30px; height: 30px;
    background: #1a1a1a;
    border: 1px solid #242424;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 0.85rem; cursor: pointer;
    transition: all 0.2s ease;
}
.card-icon-circle:hover { background: #222; border-color: var(--accent); }

.top-nav-bar { display: none !important; }

/* ─── STAGE 3 VOICE CARDS RED THEME ALIGNMENT ─── */
.stage3-container div[data-testid="column"] {
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0 !important;
}
.voice-card-wrapper {
    background-color: var(--inner-bg) !important;
    border-radius: 6px !important;
    padding: 4px 8px !important;
    margin-bottom: 4px !important;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1) !important;
    border: 1px solid rgba(255, 255, 255, 0.05) !important;
    transition: all 0.2s ease !important;
    display: block !important;
    min-height: 34px !important;
    box-sizing: border-box !important;
}
.stage3-container iframe {
    display: none !important;
}
.stage3-container div[data-testid="element-container"],
.stage3-container div.element-container {
    min-height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
}
.stage3-container div[data-testid="column"] {
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    min-height: 28px !important;
}
.stage3-container div[data-testid="column"]:nth-child(1) {
    justify-content: flex-start !important;
}
.stage3-container div[data-testid="stAudio"],
.stage3-container div.stAudio {
    margin: 0 !important;
    padding: 0 !important;
    height: 28px !important;
    min-height: 28px !important;
    background: transparent !important;
    display: flex !important;
    align-items: center !important;
    width: 100% !important;
}
.stage3-container div[data-testid="stAudio"] audio,
.stage3-container div.stAudio audio {
    height: 28px !important;
    width: 100% !important;
    border-radius: 6px !important;
}
.voice-card-wrapper:hover {
    border-color: rgba(255, 255, 255, 0.12) !important;
}
.voice-card-wrapper.selected {
    border: 1px solid var(--accent) !important;
    box-shadow: 0 0 6px var(--accent-glow) !important;
    background-color: rgba(139, 26, 26, 0.06) !important;
}
.voice-card-text {
    display: flex !important;
    flex-direction: column !important;
    justify-content: center !important;
    min-height: 26px !important;
    text-align: right !important;
}
.voice-card-name {
    font-size: 0.70rem !important;
    font-weight: 700 !important;
    color: #ffffff !important;
    margin-bottom: 0px !important;
    line-height: 1.1 !important;
}
.voice-card-desc {
    font-size: 0.55rem !important;
    color: #94a3b8 !important;
    line-height: 1.1 !important;
}
.voice-play-circle-btn {
    background: rgba(255, 255, 255, 0.05) !important;
    border: 1px solid rgba(255, 255, 255, 0.15) !important;
    color: #e2e8f0 !important;
    border-radius: 50% !important;
    width: 26px !important;
    height: 26px !important;
    font-size: 0.75rem !important;
    cursor: pointer !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    transition: all 0.2s ease !important;
    outline: none !important;
    padding: 0 !important;
    box-sizing: border-box !important;
}
.voice-play-circle-btn:hover {
    background: var(--accent) !important;
    border-color: var(--accent) !important;
    color: #ffffff !important;
    transform: scale(1.1) !important;
}

/* Custom voice selector button inside the card */
.stage3-container div[data-testid="column"]:nth-child(3) div.stButton > button {
    height: 26px !important;
    min-height: 26px !important;
    width: 26px !important;
    border-radius: 50% !important;
    font-size: 0.75rem !important;
    font-weight: 700 !important;
    background: rgba(255, 255, 255, 0.04) !important;
    color: #ffffff !important;
    border: 1px solid rgba(255, 255, 255, 0.12) !important;
    transition: all 0.2s ease !important;
    display: flex !important;
    justify-content: center !important;
    align-items: center !important;
    padding: 0 !important;
    margin-top: 0px !important;
}
.stage3-container div[data-testid="column"]:nth-child(3) div.stButton > button:hover:not(:disabled) {
    background: var(--accent) !important;
    border-color: var(--accent) !important;
    box-shadow: 0 0 6px var(--accent-glow) !important;
}
.stage3-container div[data-testid="column"]:nth-child(3) div.stButton > button:disabled {
    background: linear-gradient(135deg, var(--accent) 0%, var(--accent-dark) 100%) !important;
    color: #ffffff !important;
    border: none !important;
    box-shadow: 0 2px 8px var(--accent-glow) !important;
    cursor: default !important;
    opacity: 1 !important;
}

/* Subtitle style buttons */
div.sub-style-btn-marker + div.stButton > button {
    background-color: var(--inner-bg) !important;
    border: 1px solid rgba(255, 255, 255, 0.04) !important;
    color: #94a3b8 !important;
    font-size: 0.78rem !important;
    font-weight: 600 !important;
    border-radius: 8px !important;
    height: 40px !important;
    transition: all 0.2s ease !important;
    box-shadow: 0 4px 10px rgba(0, 0, 0, 0.15) !important;
}
div.sub-style-btn-marker + div.stButton > button:hover {
    border-color: rgba(255, 255, 255, 0.15) !important;
    color: #ffffff !important;
    transform: translateY(-0.5px) !important;
}
div.sub-style-btn-marker.selected.green + div.stButton > button {
    border-color: #10b981 !important;
    background-color: rgba(16, 185, 129, 0.05) !important;
    color: #ffffff !important;
    box-shadow: 0 0 10px rgba(16, 185, 129, 0.15) !important;
}
div.sub-style-btn-marker.selected.blue + div.stButton > button {
    border-color: #3b82f6 !important;
    background-color: rgba(59, 130, 246, 0.05) !important;
    color: #ffffff !important;
    box-shadow: 0 0 10px rgba(59, 130, 246, 0.15) !important;
}

/* ─── STAGE 4 GREEN TO RED ALIGNMENTS ─── */
.stage4-container div[data-testid="stColumn"]:nth-child(2) div[data-testid="stNotification"],
.stage4-container div[data-testid="column"]:nth-child(2) div[data-testid="stNotification"] {
    background-color: var(--accent-soft) !important;
    border: 1px solid var(--accent-border) !important;
    color: var(--accent) !important;
}
.stage4-container div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTextArea"] textarea:focus,
.stage4-container div[data-testid="column"]:nth-child(2) div[data-testid="stTextArea"] textarea:focus {
    border-color: var(--accent) !important;
}

/* Collapse the Streamlit HTML element container and make the background iframe cover the screen */
div.element-container:has(iframe:not([height="30"]):not([height="65"])) {
    position: absolute !important;
    height: 0px !important;
    width: 0px !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: visible !important;
    z-index: -10 !important;
}
div.stHtml:has(iframe:not([height="30"]):not([height="65"])) {
    height: 0px !important;
    width: 0px !important;
    margin: 0 !important;
    padding: 0 !important;
}
iframe:not([height="30"]):not([height="65"]) {
    position: fixed !important;
    top: 0 !important;
    left: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    z-index: -10 !important;
    pointer-events: none !important;
    border: none !important;
    background: transparent !important;
}

div.stHtml:has(iframe[height="30"]) {
    height: 30px !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
}
iframe[height="30"] {
    width: 100% !important;
    height: 30px !important;
    border: none !important;
    background: transparent !important;
}

/* ─── STAGE 2 STYLING ─── */
.stage2-title {
    font-family: 'Outfit', sans-serif !important;
    font-size: 1.05rem !important;
    font-weight: 800 !important;
    text-align: center !important;
    color: #ffffff !important;
    margin-bottom: 12px !important;
    line-height: 1.45 !important;
    direction: rtl !important;
}
.stage2-container div[data-testid="stHorizontalBlock"] {
    flex-direction: row !important;
    flex-wrap: nowrap !important;
    width: 100% !important;
    gap: 12px !important;
    display: flex !important;
    overflow-x: auto !important;
    padding-bottom: 10px !important;
    margin-bottom: 8px !important;
}
.stage2-container div[data-testid="stHorizontalBlock"]::-webkit-scrollbar {
    height: 4px !important;
}
.stage2-container div[data-testid="stHorizontalBlock"]::-webkit-scrollbar-thumb {
    background: rgba(255, 255, 255, 0.22) !important;
    border-radius: 10px !important;
}
.stage2-container div[data-testid="column"] {
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0px !important;
    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    position: relative !important;
    margin-bottom: 8px !important;
    
    /* Fixed 5.2cm width for each column wrapper */
    width: 5.2cm !important;
    min-width: 5.2cm !important;
    max-width: 5.2cm !important;
    flex-grow: 0 !important;
    flex-shrink: 0 !important;
}
.stage2-container div[data-testid="column"]:has(.selected-column-marker) .tiktok-video {
    border: 2px solid var(--accent) !important;
    box-shadow: 0 0 15px var(--accent-glow) !important;
}

/* Force video to render in TikTok aspect ratio 9:16 portrait style, size 5.2cm x 10.5cm */
video.tiktok-video {
    width: 5.2cm !important;
    height: 10.5cm !important;
    max-height: none !important;
    object-fit: cover !important;
    border-radius: 16px !important;
    border: 1px solid rgba(255, 255, 255, 0.08) !important;
    background: #111111 !important;
    display: block !important;
}

/* TikTok Badge Overlay */
.video-card-overlay::before {
    content: "TikTok";
    position: absolute;
    top: 14px;
    left: 14px;
    background: rgba(0, 0, 0, 0.75);
    color: #ffffff;
    font-size: 0.58rem;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 4px;
    z-index: 5;
    font-family: 'Inter', sans-serif;
    pointer-events: none;
}

/* Green Checkmark Badge Overlay for selected card */
.video-card-overlay.selected::after {
    content: "✓";
    position: absolute;
    top: 14px;
    right: 14px;
    width: 18px;
    height: 18px;
    background: #10b981; /* Green checkmark */
    color: #ffffff;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.72rem;
    font-weight: 900;
    z-index: 5;
    pointer-events: none;
    box-shadow: 0 2px 6px rgba(0,0,0,0.4);
}

.video-card-desc {
    font-size: 0.72rem !important;
    font-weight: 500 !important;
    color: #e2e8f0 !important;
    line-height: 1.35 !important;
    margin-top: 8px !important;
    margin-bottom: 3px !important;
    max-height: 50px !important;
    overflow: hidden !important;
    display: -webkit-box !important;
    -webkit-line-clamp: 2 !important;
    -webkit-box-orient: vertical !important;
    text-align: center !important;
    width: 100% !important;
    padding: 0 4px !important;
}
.video-card-views {
    font-size: 0.65rem !important;
    color: #888888 !important;
    margin-bottom: 2px !important;
    text-align: center !important;
    width: 100% !important;
}

    .stage3-container div.back-btn-container div.stButton > button {
        background: #121212 !important;
        color: #ffffff !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important;
        border-radius: 8px !important;
    }
    .stage3-container div.back-btn-container div.stButton > button:hover {
        background: #1c1c1c !important;
        border-color: rgba(255, 255, 255, 0.25) !important;
    }

    /* Primary action buttons styled as red gradient pills */
    div.element-container:has(.primary-btn-marker) + div.element-container div.stButton > button,
    div.element-container:has(.primary-btn-marker) + div.element-container div.stDownloadButton > button {
        background: linear-gradient(135deg, var(--accent) 0%, var(--accent-dark) 100%) !important;
        color: #ffffff !important;
        border: none !important;
        border-radius: 20px !important;
        font-size: 0.8rem !important;
        font-weight: 700 !important;
        height: 38px !important;
        box-shadow: 0 4px 18px var(--accent-glow) !important;
        letter-spacing: 0.3px !important;
        transition: all 0.2s ease !important;
        width: 100% !important;
        display: inline-flex !important;
        justify-content: center !important;
        align-items: center !important;
    }
    div.element-container:has(.primary-btn-marker) + div.element-container div.stButton > button:hover,
    div.element-container:has(.primary-btn-marker) + div.element-container div.stDownloadButton > button:hover {
        background: linear-gradient(135deg, #9e2020 0%, #7a1515 100%) !important;
        box-shadow: 0 6px 22px var(--accent-glow) !important;
        transform: translateY(-1px) !important;
    }
    div.element-container:has(.primary-btn-marker) + div.element-container div.stButton > button:active,
    div.element-container:has(.primary-btn-marker) + div.element-container div.stDownloadButton > button:active {
        transform: translateY(0px) !important;
        box-shadow: 0 2px 10px var(--accent-glow) !important;
    }

    /* Secondary / back action buttons styled as dark bordered rectangles */
    div.element-container:has(.secondary-btn-marker) + div.element-container div.stButton > button,
    div.element-container:has(.secondary-btn-marker) + div.element-container div.stDownloadButton > button {
        background: #121212 !important;
        color: #ffffff !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important;
        border-radius: 8px !important;
        font-size: 0.8rem !important;
        font-weight: 600 !important;
        height: 38px !important;
        box-shadow: none !important;
        transition: all 0.2s ease !important;
        width: 100% !important;
        display: inline-flex !important;
        justify-content: center !important;
        align-items: center !important;
    }
    div.element-container:has(.secondary-btn-marker) + div.element-container div.stButton > button:hover,
    div.element-container:has(.secondary-btn-marker) + div.element-container div.stDownloadButton > button:hover {
        background: #1c1c1c !important;
        border-color: rgba(255, 255, 255, 0.25) !important;
    }
    
    /* ─── PROGRESS BAR ─── */
    .progress-container {
        background: rgba(255, 255, 255, 0.02) !important;
        border: 1px solid rgba(255, 255, 255, 0.06) !important;
        border-radius: 10px !important;
        padding: 12px 16px !important;
        margin: 10px 0 !important;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15) !important;
    }
    .progress-bar-wrapper {
        background: rgba(255, 255, 255, 0.05) !important;
        border-radius: 4px !important;
        height: 6px !important;
        overflow: hidden !important;
        margin-bottom: 6px !important;
    }
    .progress-bar-fill {
        background: linear-gradient(90deg, #e11d48, #f43f5e) !important;
        height: 100% !important;
        transition: width 0.3s ease !important;
    }
    .progress-text-row {
        display: flex !important;
        justify-content: space-between !important;
        align-items: center !important;
        font-size: 0.75rem !important;
        color: #94a3b8 !important;
        direction: rtl !important;
    }
    .progress-percent {
        font-weight: bold !important;
        color: #f43f5e !important;
    }
    .progress-label {
        font-weight: 500 !important;
    }
    </style>
""", unsafe_allow_html=True)

components.html(r"""
<!DOCTYPE html>
<html>
<head>
<style>
html, body {
    margin: 0 !important;
    padding: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    background: transparent !important;
    overflow: hidden !important;
}
#plexus-canvas {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    z-index: 1;
}
</style>
</head>
<body>
<canvas id="plexus-canvas"></canvas>

<script>
    const canvas = document.getElementById('plexus-canvas');
    const ctx = canvas.getContext('2d');
    
    let width = 0;
    let height = 0;
    
    const particles = [];
    const embers = [];
    const emberCount = 30;
    let sceneInitialized = false;
    
    class Particle {
        constructor() {
            this.x = Math.random() * width;
            this.y = Math.random() * height;
            this.radius = Math.random() * 2 + 0.8;
            this.vx = (Math.random() - 0.5) * 0.35;
            this.vy = (Math.random() - 0.5) * 0.35;
            // 70% Silver, 30% Crimson Red
            this.baseColor = Math.random() < 0.3 ? '220, 20, 60' : '192, 192, 192';
            this.alpha = Math.random() * 0.6 + 0.2;
        }
        update() {
            this.x += this.vx;
            this.y += this.vy;
            if (this.x < 0 || this.x > width) this.vx *= -1;
            if (this.y < 0 || this.y > height) this.vy *= -1;
        }
        draw() {
            ctx.beginPath();
            ctx.arc(this.x, this.y, this.radius, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(${this.baseColor}, ${this.alpha})`;
            if (this.baseColor === '220, 20, 60') {
                ctx.shadowBlur = 6;
                ctx.shadowColor = `rgba(220, 20, 60, ${this.alpha * 0.8})`;
            } else {
                ctx.shadowBlur = 4;
                ctx.shadowColor = `rgba(192, 192, 192, ${this.alpha * 0.5})`;
            }
            ctx.fill();
            ctx.shadowBlur = 0;
        }
    }
    
    class Ember {
        constructor() {
            this.reset();
            this.y = Math.random() * height;
        }
        reset() {
            this.x = Math.random() * width;
            this.y = height + 15;
            this.radius = Math.random() * 2.2 + 0.6;
            this.vy = -(Math.random() * 0.7 + 0.25);
            this.vx = (Math.random() - 0.5) * 0.25;
            this.alpha = Math.random() * 0.55 + 0.15;
            this.decay = Math.random() * 0.0018 + 0.0008;
            this.color = Math.random() < 0.25 ? '192, 192, 192' : '139, 26, 26';
        }
        update() {
            this.x += this.vx;
            this.y += this.vy;
            this.alpha -= this.decay;
            if (this.alpha <= 0 || this.y < -10 || this.x < -10 || this.x > width + 10) {
                this.reset();
            }
        }
        draw() {
            ctx.beginPath();
            ctx.arc(this.x, this.y, this.radius, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(${this.color}, ${this.alpha})`;
            if (this.color !== '192, 192, 192') {
                ctx.shadowBlur = 8;
                ctx.shadowColor = `rgba(139, 26, 26, ${this.alpha})`;
            } else {
                ctx.shadowBlur = 4;
                ctx.shadowColor = `rgba(192, 192, 192, ${this.alpha})`;
            }
            ctx.fill();
            ctx.shadowBlur = 0;
        }
    }
    
    function initScene() {
        particles.length = 0;
        const particleCount = Math.min(85, Math.floor((width * height) / 14000)) || 40;
        for (let i = 0; i < particleCount; i++) {
            particles.push(new Particle());
        }
        
        embers.length = 0;
        for (let i = 0; i < emberCount; i++) {
            embers.push(new Ember());
        }
        
        sceneInitialized = true;
    }
    
    let auras = [];
    function initAuras() {
        auras = [
            { x: width * 0.3, y: height * 0.3, radius: Math.max(width, height) * 0.35, vx: 0.1, vy: 0.08, color: '139, 26, 26', maxAlpha: 0.09 },
            { x: width * 0.7, y: height * 0.6, radius: Math.max(width, height) * 0.4, vx: -0.08, vy: -0.12, color: '90, 90, 100', maxAlpha: 0.06 }
        ];
    }
    
    function connect() {
        const maxDistance = 115;
        for (let i = 0; i < particles.length; i++) {
            for (let j = i + 1; j < particles.length; j++) {
                const dx = particles[i].x - particles[j].x;
                const dy = particles[i].y - particles[j].y;
                const dist = Math.sqrt(dx * dx + dy * dy);
                
                if (dist < maxDistance) {
                    const alpha = (1 - (dist / maxDistance)) * 0.22;
                    ctx.beginPath();
                    ctx.moveTo(particles[i].x, particles[i].y);
                    ctx.lineTo(particles[j].x, particles[j].y);
                    
                    const grad = ctx.createLinearGradient(particles[i].x, particles[i].y, particles[j].x, particles[j].y);
                    grad.addColorStop(0, `rgba(${particles[i].baseColor}, ${alpha})`);
                    grad.addColorStop(1, `rgba(${particles[j].baseColor}, ${alpha})`);
                    
                    ctx.strokeStyle = grad;
                    ctx.lineWidth = 0.8;
                    ctx.stroke();
                }
            }
        }
    }
    
    function animate() {
        if (canvas.width !== window.innerWidth || canvas.height !== window.innerHeight || !sceneInitialized) {
            width = canvas.width = window.innerWidth;
            height = canvas.height = window.innerHeight;
            if (width > 100 && height > 100) {
                initScene();
                initAuras();
            }
        }
        
        if (!sceneInitialized) {
            requestAnimationFrame(animate);
            return;
        }
        
        ctx.clearRect(0, 0, width, height);
        
        // Base dark radial gradient background
        const radial = ctx.createRadialGradient(width/2, height/2, 10, width/2, height/2, Math.max(width, height) * 0.85);
        radial.addColorStop(0, '#090202');
        radial.addColorStop(0.5, '#020000');
        radial.addColorStop(1, '#000000');
        ctx.fillStyle = radial;
        ctx.fillRect(0, 0, width, height);
        
        // Render dynamic shifting auras
        auras.forEach(a => {
            a.x += a.vx;
            a.y += a.vy;
            if (a.x < a.radius * 0.1 || a.x > width - a.radius * 0.1) a.vx *= -1;
            if (a.y < a.radius * 0.1 || a.y > height - a.radius * 0.1) a.vy *= -1;
            
            const grad = ctx.createRadialGradient(a.x, a.y, 0, a.x, a.y, a.radius);
            grad.addColorStop(0, `rgba(${a.color}, ${a.maxAlpha})`);
            grad.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = grad;
            ctx.beginPath();
            ctx.arc(a.x, a.y, a.radius, 0, Math.PI * 2);
            ctx.fill();
        });
        
        connect();
        
        particles.forEach(p => {
            p.update();
            p.draw();
        });
        
        embers.forEach(e => {
            e.update();
            e.draw();
        });
        
        requestAnimationFrame(animate);
    }
    animate();

    try {
        const doc = window.parent.document;
        const parentWin = window.parent;
        
        function loadScript(src, callback) {
            if (doc.querySelector(`script[src="${src}"]`)) {
                if (callback) callback();
                return;
            }
            const script = doc.createElement('script');
            script.src = src;
            script.onload = callback;
            doc.head.appendChild(script);
        }

        loadScript("https://cdn.tailwindcss.com");
        loadScript("https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.2/gsap.min.js", () => {
            const gsap = parentWin.gsap;
            if (!gsap) return;
            
            if (parentWin.__dragonObserver) {
                parentWin.__dragonObserver.disconnect();
            }
            
            const observer = new MutationObserver((mutations) => {
                mutations.forEach((mutation) => {
                    mutation.addedNodes.forEach((node) => {
                        if (node.nodeType === 1) {
                            const card = node.querySelector('.stage1-card, .stage2-container, .stage3-container, .stage4-container, div:has(.card-wrapper) + div');
                            if (card) {
                                let currentStage = "";
                                if (card.classList.contains('stage1-card')) currentStage = "stage1";
                                else if (card.classList.contains('stage2-container')) currentStage = "stage2";
                                else if (card.classList.contains('stage3-container')) currentStage = "stage3";
                                else if (card.classList.contains('stage4-container')) currentStage = "stage4";
                                
                                if (currentStage) {
                                    if (parentWin.__lastGSAPStage === currentStage) {
                                        return;
                                    }
                                    parentWin.__lastGSAPStage = currentStage;
                                } else if (card.classList.contains('gsap-done')) {
                                    return;
                                }
                                
                                card.classList.add('gsap-done');
                                gsap.fromTo(card, 
                                    { opacity: 0, y: 30, scale: 0.96 }, 
                                    { opacity: 1, y: 0, scale: 1, duration: 0.8, ease: "back.out(1.2)" }
                                );
                            }
                            
                            const rows = node.querySelectorAll('.url-input-row');
                            if (rows.length > 0 && !rows[0].classList.contains('gsap-done')) {
                                rows.forEach(r => r.classList.add('gsap-done'));
                                gsap.fromTo(rows, 
                                    { opacity: 0, x: -20 }, 
                                    { opacity: 1, x: 0, duration: 0.4, stagger: 0.08, ease: "power2.out" }
                                );
                            }
                            
                            const buttons = node.querySelectorAll('div.stButton > button, div.stDownloadButton > button');
                            buttons.forEach(btn => {
                                if (!btn.classList.contains('gsap-hover-done')) {
                                    btn.classList.add('gsap-hover-done');
                                    btn.addEventListener('mouseenter', () => {
                                        gsap.to(btn, { scale: 1.04, duration: 0.25, ease: "power2.out" });
                                    });
                                    btn.addEventListener('mouseleave', () => {
                                        gsap.to(btn, { scale: 1, duration: 0.2, ease: "power2.out" });
                                    });
                                }
                            });
                        }
                    });
                });
            });
            observer.observe(doc.body, { childList: true, subtree: true });
            parentWin.__dragonObserver = observer;
        });
    } catch (e) {
        console.warn("GSAP parent observer skipped due to sandbox limits:", e);
    }
</script>
</body>
</html>
""", height=10)


if bg_img_css:
    st.markdown(f"""
    <style>
    html, body, [data-testid="stAppViewContainer"], .main, .stApp {{
        {bg_img_css}
        background-size: 130% 130% !important;
        background-position: center !important;
        background-repeat: no-repeat !important;
        animation: panBackground 24s ease-in-out infinite !important;
    }}
    
    @keyframes panBackground {{
        0% {{
            background-position: 50% 50%;
            filter: brightness(0.85);
        }}
        50% {{
            background-position: 55% 45%;
            filter: brightness(1.05) contrast(1.02);
        }}
        100% {{
            background-position: 50% 50%;
            filter: brightness(0.85);
        }}
    }}
    </style>
    """, unsafe_allow_html=True)


if "templates_built" not in st.session_state:
    st.session_state.templates_built = False
if "compilation_done" not in st.session_state:
    st.session_state.compilation_done = False
if "voice_selected" not in st.session_state:
    st.session_state.voice_selected = False
if "voiceover_generated" not in st.session_state:
    st.session_state.voiceover_generated = False

if not st.session_state.templates_built:
    active_stage = 1
elif not st.session_state.compilation_done:
    active_stage = 2
elif not st.session_state.get("voice_selected", False):
    active_stage = 3
elif not st.session_state.get("voiceover_generated", False):
    active_stage = 4
else:
    active_stage = 5

# ── 5-STEP INDICATOR ──
if active_stage == 1:
    active_circle = 1
    step_subtitle = "الخطوة الأولى من أصل 5 - الروابط"
elif active_stage == 2:
    active_circle = 2
    step_subtitle = "الخطوة الثانية من أصل 5 - معاينة وتجميع القوالب"
elif active_stage == 3:
    active_circle = 3
    step_subtitle = "الخطوة الثالثة من أصل 5 - اختيار الصوت"
elif active_stage == 4:
    active_circle = 4
    step_subtitle = "الخطوة الرابعة من أصل 5 - تعديل السكربت والصوت"
else:
    active_circle = 5
    step_subtitle = "الخطوة الخامسة من أصل 5 - التحميل"

circles_html = "".join(
    f'<div class="step-circle {"done" if i < active_circle else ("active" if i == active_circle else "inactive")}">{i}</div>'
    for i in range(1, 6)
)
st.markdown(
    f"""
    <div class="steps-container">
        {circles_html}
    </div>
    <div class="step-subtitle">{step_subtitle}</div>
    """,
    unsafe_allow_html=True
)

if active_stage == 1:
    # init link counter
    if "num_links" not in st.session_state:
        st.session_state.num_links = 2

    st.markdown('<div class="stage1-container stage1-card">', unsafe_allow_html=True)
    st.markdown('<div class="stage1-title">🎬 استوديو تجميع الفيديو</div>', unsafe_allow_html=True)
    st.markdown('<div class="step-subtitle" style="text-align: center; color: #a1a1aa; font-size: 0.8rem; margin-bottom: 12px;">أدخل روابط فيديوهات TikTok لإنشاء القوالب</div>', unsafe_allow_html=True)

    with st.container():
        urls = []
        for i in range(st.session_state.num_links):
            key = f"url_{i}"
            url_val = st.session_state.get(key, "")
            st.markdown(
                f'<div class="url-input-row">'
                f'<span class="url-chain-icon">🔗</span>',
                unsafe_allow_html=True
            )
            url = st.text_input(
                f"الرابط {i+1}", key=key,
                placeholder="أدخل رابط فيديو تيك توك هنا...",
                label_visibility="collapsed"
            )
            st.markdown('</div>', unsafe_allow_html=True)
            if url.strip():
                urls.append(url.strip())

        # Add link button (dash / outline style)
        st.markdown('<div class="secondary-btn-marker"></div>', unsafe_allow_html=True)
        if st.button("➕ إضافة رابط آخر", use_container_width=True, key="add_link_btn"):
            if st.session_state.num_links < 5:
                st.session_state.num_links += 1
                st.rerun()

        # Spacer
        st.markdown('<div style="height: 10px;"></div>', unsafe_allow_html=True)

        # Generate button inside card (centered, premium red pill)
        st.markdown('<div class="primary-btn-marker"></div>', unsafe_allow_html=True)
        run_btn = st.button("إنشاء قوالب الفيديو ⚡", use_container_width=True, key="generate_btn")

        output_name = "compiled_output.mp4"

        if run_btn:
            st.session_state.compilation_done = False
            st.session_state.templates_built = False
            st.session_state.template_paths = []
            st.session_state.built_urls = []
            if not urls:
                st.error("الرجاء إدخال رابط فيديو واحد على الأقل.")
                st.stop()

            status_placeholder = st.empty()
            st.session_state.download_status = {"percent": 0, "label": "جاري بدء التحميل... 0%"}
            
            def progress_pct_cb(pct, label):
                st.session_state.download_status = {"percent": pct, "label": label}

            try:
                template_paths = []
                error_container = []

                def run_async():
                    try:
                        paths = download_all_raw_videos_async(
                            urls,
                            progress_cb=None,
                            progress_percent_cb=progress_pct_cb
                        )
                        template_paths.extend(paths)
                    except Exception as e:
                        error_container.append(e)

                build_thread = threading.Thread(target=run_async)
                build_thread.start()

                while build_thread.is_alive():
                    status_placeholder.markdown(
                        get_custom_progress_bar_html(
                            st.session_state.download_status["percent"],
                            st.session_state.download_status["label"]
                        ),
                        unsafe_allow_html=True
                    )
                    time.sleep(0.1)

                if error_container:
                    raise error_container[0]

                # Ensure 100% is displayed at the end
                status_placeholder.markdown(
                    get_custom_progress_bar_html(100, "تم التحميل بنجاح ✓ 100%"),
                    unsafe_allow_html=True
                )
                time.sleep(0.5)

                st.session_state.template_paths = template_paths
                st.session_state.built_urls = list(urls)
                st.session_state.templates_built = True
                st.rerun()

            except Exception as err:
                st.error(f"❌ خطأ: {err}")
    st.markdown('</div>', unsafe_allow_html=True)

elif active_stage == 2:
    st.markdown('<div class="stage2-container">', unsafe_allow_html=True)
    st.markdown('<div class="stage2-title">معاينة وتجميع القوالب</div>', unsafe_allow_html=True)

    urls = [st.session_state.get(f"url_{i}", "") for i in range(st.session_state.get("num_links", 2))]
    urls = [u.strip() for u in urls if u.strip()]
    urls_changed = (urls != st.session_state.built_urls)


    if "selected_preview_idx" not in st.session_state:
        st.session_state.selected_preview_idx = 0

    if st.session_state.template_paths:
        # Columns for horizontal layout of all videos
        cols = st.columns(len(st.session_state.template_paths))
        for idx, preview_path in enumerate(st.session_state.template_paths):
            with cols[idx]:
                is_selected = (st.session_state.selected_preview_idx == idx)
                if is_selected:
                    st.markdown('<div class="selected-column-marker"></div>', unsafe_allow_html=True)
                    st.markdown('<div class="video-card-overlay selected"></div>', unsafe_allow_html=True)
                else:
                    st.markdown('<div class="video-card-overlay"></div>', unsafe_allow_html=True)
                if os.path.exists(preview_path):
                    # Extract thumbnail image if missing (on-demand for backward compatibility)
                    thumb_path = os.path.splitext(preview_path)[0] + "_thumb.jpg"
                    if not os.path.exists(thumb_path):
                        extract_thumbnail(preview_path, thumb_path)
                        
                    # Copy thumbnail to Streamlit static directory
                    import shutil
                    st_static_path = os.path.join(os.path.dirname(st.__file__), "static")
                    app_media_dir = os.path.join(st_static_path, "app_media")
                    os.makedirs(app_media_dir, exist_ok=True)
                    
                    thumb_filename = os.path.basename(thumb_path)
                    static_dest = os.path.join(app_media_dir, thumb_filename)
                    if not os.path.exists(static_dest):
                        try:
                            shutil.copy2(thumb_path, static_dest)
                        except Exception:
                            pass
                            
                    mtime = int(os.path.getmtime(preview_path))
                    st.markdown(
                        f"""
                        <img class="tiktok-video" src="app_media/{thumb_filename}?v={mtime}"
                             style="width:100%; max-width:5.2cm; height:10.5cm; object-fit:cover; border-radius:16px; display:block; background:#111; box-shadow: 0 4px 12px rgba(0,0,0,0.5);">
                        """,
                        unsafe_allow_html=True
                    )
                else:
                    st.error("غير موجود")
                
                # Fetch metadata JSON (video descriptions and views)
                metadata_path = os.path.splitext(preview_path)[0] + ".json"
                metadata = {}
                if os.path.exists(metadata_path):
                    try:
                        with open(metadata_path, "r", encoding="utf-8") as f_meta:
                            metadata = json.load(f_meta)
                    except Exception:
                        pass
                
                desc = metadata.get("title") or metadata.get("description") or ""
                views = metadata.get("view_count", 0)
                
                if views:
                    try:
                        views_val = int(views)
                        if views_val >= 1000000:
                            views_str = f"{views_val/1000000:.1f}M vues"
                        elif views_val >= 1000:
                            views_str = f"{views_val/1000:.1f}K vues"
                        else:
                            views_str = f"{views_val} vues"
                    except Exception:
                        views_str = f"{views} vues"
                else:
                    views_str = "0 vues"
                
                if desc or views_str:
                    st.markdown(
                        f"""
                        <div class="video-card-desc">{desc}</div>
                        <div class="video-card-views">{views_str}</div>
                        """,
                        unsafe_allow_html=True
                    )
                
                btn_label = "👁️ معاينة" if not is_selected else "✅ محدد"
                st.markdown('<div class="secondary-btn-marker"></div>', unsafe_allow_html=True)
                if st.button(btn_label, key=f"select_preview_{idx}", disabled=is_selected, use_container_width=True):
                    st.session_state.selected_preview_idx = idx
                    st.rerun()


    st.markdown("<hr style='margin: 10px 0; border-color: rgba(255,255,255,0.06);'>", unsafe_allow_html=True)
    st.markdown('<div class="primary-btn-marker"></div>', unsafe_allow_html=True)
    compile_btn = st.button("🎬 تجميع الفيديو النهائي", use_container_width=True)
    if compile_btn:
        output_name = st.session_state.get("output_name") or "compiled_output.mp4"
        output_path = os.path.abspath(output_name)
        temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_processing")
        os.makedirs(temp_dir, exist_ok=True)

        st.session_state.compilation_status = {
            "in_progress": True,
            "done": False,
            "success": False,
            "percent": 5,
            "label": "بدء التعديل والتجميع... 5%",
            "logs": [],
            "error": None,
            "output_path": "",
            "output_name": ""
        }
        
        selected_idx = st.session_state.get("selected_preview_idx", 0)

        comp_thread = threading.Thread(
            target=run_bg_compilation,
            args=(
                st.session_state.template_paths,
                st.session_state.built_urls,
                output_name,
                output_path,
                temp_dir,
                st.session_state.compilation_status,
                selected_idx
            )
        )
        comp_thread.daemon = True
        comp_thread.start()

        # Remain in Stage 2 for a short moment to show progress starting
        status_placeholder = st.empty()
        for pct in [10, 15, 20]:
            lbl = f"جاري بدء التجميع... {pct}%"
            st.session_state.compilation_status["percent"] = pct
            st.session_state.compilation_status["label"] = lbl
            write_progress(pct, lbl)
            status_placeholder.markdown(
                get_custom_progress_bar_html(pct, lbl),
                unsafe_allow_html=True
            )
            time.sleep(0.4)

        st.session_state.compilation_done = True
        st.rerun()

    st.markdown('<div class="back-btn-container" style="margin-top: 10px;">', unsafe_allow_html=True)
    st.markdown('<div class="secondary-btn-marker"></div>', unsafe_allow_html=True)
    if st.button("⬅️ رجوع — تحميل فيديوهات جديدة", key="reset_btn_stage2", use_container_width=True):
        cleanup_temporary_files(force=True, clean_root=True)
        st.session_state.templates_built = False
        st.session_state.compilation_done = False
        st.session_state.template_paths = []
        st.session_state.built_urls = []
        st.session_state.uploaded_audio_path = ""
        st.session_state.uploaded_audio_name = ""
        st.session_state.mixed_output_path = ""
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

elif active_stage == 3:
    fallback_voices = {
        "5Qfm4RqcAer0xoyWtoHC": {
            "name": "Maxime - Young and Casual",
            "voice_id": "5Qfm4RqcAer0xoyWtoHC",
            "description": "صوت فرنسي شبابي طبيعي (موصى به)",
            "preview_url": "https://storage.googleapis.com/eleven-public-prod/5ZLt0Hmg2VRLLnemw1a6hS3wns93/voices/5Qfm4RqcAer0xoyWtoHC/77822fe9-c70b-4902-80b8-b5a77baac8c6.mp3"
        },
        "7Pm7442WzqlfkW9vjmO9": {
            "name": "Maxime - Dynamic and Natural",
            "voice_id": "7Pm7442WzqlfkW9vjmO9",
            "description": "صوت فرنسي ديناميكي وحيوي",
            "preview_url": "https://storage.googleapis.com/eleven-public-prod/database/workspace/fe41dc2febc5427d922e4f111a72b264/voices/7Pm7442WzqlfkW9vjmO9/55Gh4hV0CP1rJNWXZIWw.mp3"
        },
        "8BRNClOqAj0d70eCur4i": {
            "name": "SimoneF - Advertising & Social Media",
            "voice_id": "8BRNClOqAj0d70eCur4i",
            "description": "صوت فرنسي إعلاني احترافي",
            "preview_url": "https://storage.googleapis.com/eleven-public-prod/database/workspace/d0ddb5b7774b466fa6a44d4965e29a2b/voices/8BRNClOqAj0d70eCur4i/f425d072-395d-46f4-a40f-f325965e0228.mp3"
        },
        "BVBq6HVJVdnwOMJOqvy9": {
            "name": "Nova - Deep and Calm",
            "voice_id": "BVBq6HVJVdnwOMJOqvy9",
            "description": "صوت فرنسي هادئ وعميق",
            "preview_url": "https://storage.googleapis.com/eleven-public-prod/database/workspace/1f611c60043e461a9368bbc7d9ebf490/voices/BVBq6HVJVdnwOMJOqvy9/KGUSSS3qwOxJcQKsP2rA.mp3"
        },
        "cr4ZamHlgxGWjZ2h6X2b": {
            "name": "Tarek - Narrative Strong",
            "voice_id": "cr4ZamHlgxGWjZ2h6X2b",
            "description": "صوت سردي قوي وواضح",
            "preview_url": "https://storage.googleapis.com/eleven-public-prod/database/workspace/f9cc8d609b64462ea9de4ac2e9f81849/voices/cr4ZamHlgxGWjZ2h6X2b/03660355-94e9-4acc-bbb6-76461aa1cf6d.mp3"
        },
        "YAQknamXloI8hvBnB8Dd": {
            "name": "E.GT - Crisp",
            "voice_id": "YAQknamXloI8hvBnB8Dd",
            "description": "صوت فرنسي واضح ونقي",
            "preview_url": "https://storage.googleapis.com/eleven-public-prod/database/workspace/f9cc8d609b64462ea9de4ac2e9f81849/voices/YAQknamXloI8hvBnB8Dd/8d53a840-2516-4126-a4f3-0c18a223c394.mp3"
        }
    }
    st.markdown('<div class="stage3-container">', unsafe_allow_html=True)
    comp_status = st.session_state.get("compilation_status", {})
    if comp_status.get("in_progress"):
        render_custom_progress_bar_js()
    elif comp_status.get("success"):
        render_custom_progress_bar(
            100,
            "تم تجميع وتعديل الفيديو بنجاح ✓ 100%"
        )
    st.markdown(
        """
        <div class="voice-title">
            <h3>🎙️ اختر صوت المعلق للفيديو (ElevenLabs)</h3>
        </div>
        """,
        unsafe_allow_html=True
    )

    if "selected_voice_id" not in st.session_state:
        st.session_state.selected_voice_id = "5Qfm4RqcAer0xoyWtoHC"  # Default to Maxime
        st.session_state.selected_voice_name = "Maxime - Young and Casual"

    if "subtitle_mode" not in st.session_state:
        st.session_state.subtitle_mode = "green"

    # Sync checkbox state variables from subtitle_mode
    if st.session_state.subtitle_mode == "green":
        st.session_state.sub_green = True
        st.session_state.sub_blue = False
    elif st.session_state.subtitle_mode == "blue":
        st.session_state.sub_green = False
        st.session_state.sub_blue = True
    else:
        st.session_state.sub_green = False
        st.session_state.sub_blue = False

    st.markdown(
        f"<div style='text-align: center; color: #94a3b8; font-size: 0.78rem; margin-bottom: 8px;'>الصوت المحدد حالياً: <b style='color:var(--accent);'>{st.session_state.selected_voice_name}</b></div>",
        unsafe_allow_html=True
    )

    # API keys configuration collapsible expander
    with st.expander("🎙️ مفتاح ElevenLabs API"):
        el_key = st.session_state.get("user_elevenlabs_key", "")
        if el_key:
            masked_el = el_key[:6] + "•" * 10 + el_key[-4:]
            st.markdown(
                f"""
                <div style="background-color: rgba(16, 185, 129, 0.06); border: 1px solid rgba(16, 185, 129, 0.15); border-radius: 8px; padding: 6px 12px; margin-bottom: 12px; text-align: center; direction: rtl;">
                    <span style="color: #10b981; font-weight: 700; font-size: 0.75rem;">🎙️ ElevenLabs: ✅ نشط — {masked_el}</span>
                </div>
                """,
                unsafe_allow_html=True
            )
        else:
            st.markdown(
                f"""
                <div style="background-color: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px; padding: 6px 12px; margin-bottom: 12px; text-align: center; direction: rtl;">
                    <span style="color: #a1a1aa; font-weight: 700; font-size: 0.75rem;">🎙️ ElevenLabs: يستخدم المفتاح الافتراضي</span>
                </div>
                """,
                unsafe_allow_html=True
            )

        def _save_el_key_s3():
            raw = st.session_state.get("el_key_input_s3", "").strip()
            if not raw:
                return
            st.session_state.user_elevenlabs_key = raw
            _persist_api_key("elevenlabs", raw)
            st.session_state["el_key_input_s3"] = ""
            st.toast("✅ تم حفظ مفتاح ElevenLabs بنجاح!")

        def _clear_el_key_s3():
            st.session_state.user_elevenlabs_key = ""
            _persist_api_key("elevenlabs", "")
            st.toast("🗑️ تم مسح مفتاح ElevenLabs")

        col_input, col_btn = st.columns([3.5, 1.2])
        with col_input:
            st.text_input(
                "مفتاح ElevenLabs",
                value="",
                placeholder="ألصق مفتاح ElevenLabs هنا...",
                type="password",
                key="el_key_input_s3",
                label_visibility="collapsed",
                on_change=_save_el_key_s3,
            )
        with col_btn:
            st.button(
                "🗑️ مسح",
                key="btn_clear_el_s3",
                on_click=_clear_el_key_s3,
                use_container_width=True
            )

    if st.session_state.get("user_elevenlabs_key"):
        api_key = st.session_state.user_elevenlabs_key
        badge_text = "🔑 تم ربط حساب ElevenLabs بنجاح (مفتاح مخصص نشط)"
    else:
        api_key = "a6bda3b0ff51f175dd88330b718acac592a0dbd6a9ac5135368a743179586bf3"
        badge_text = "🔑 تم ربط حساب ElevenLabs بنجاح (مفتاح افتراضي نشط)"

        st.markdown(
        f"""
        <div style="background-color: var(--accent-soft); border: 1px solid var(--accent-border); border-radius: 8px; padding: 6px 12px; margin-bottom: 12px; text-align: center; direction: rtl;">
            <span style="color: var(--accent); font-weight: 700; font-size: 0.75rem;">{badge_text}</span>
        </div>
        """,
        unsafe_allow_html=True
    )

    if "elevenlabs_voices" not in st.session_state:
        # Pre-populate with fallback voices so the UI renders instantly and is immediately fully operational!
        st.session_state.elevenlabs_voices = list(fallback_voices.values())
        st.session_state.voices_fetched = False

    if "prev_el_key" not in st.session_state:
        st.session_state.prev_el_key = api_key
    if st.session_state.prev_el_key != api_key:
        st.session_state.elevenlabs_voices = list(fallback_voices.values())
        st.session_state.voices_fetched = False
        st.session_state.prev_el_key = api_key
        if "voice_fetch_thread" in st.session_state:
            del st.session_state["voice_fetch_thread"]

    # Silently fetch ElevenLabs voices in a background thread if not already fetched
    if not st.session_state.get("voices_fetched", False):
        if "voice_fetch_thread" not in st.session_state:
            def silent_fetch():
                try:
                    fetched = get_elevenlabs_voices(api_key)
                    if fetched:
                        st.session_state.elevenlabs_voices = fetched
                        st.session_state.voices_fetched = True
                except Exception:
                    pass
            
            thread = threading.Thread(target=silent_fetch)
            thread.daemon = True
            st.session_state.voice_fetch_thread = thread
            thread.start()

    TARGET_VOICE_IDS = [
        "5Qfm4RqcAer0xoyWtoHC",  # Maxime - Young and Casual
        "7Pm7442WzqlfkW9vjmO9",  # Maxime - Dynamic and Natural
        "8BRNClOqAj0d70eCur4i",  # SimoneF - Advertising & Social Media
        "BVBq6HVJVdnwOMJOqvy9",  # Nova - Deep and Calm
        "cr4ZamHlgxGWjZ2h6X2b",  # tarek
        "YAQknamXloI8hvBnB8Dd"   # E.GT
    ]

    voices_to_display = []
    fetched_by_id = {v["voice_id"]: v for v in st.session_state.elevenlabs_voices}

    for vid in TARGET_VOICE_IDS:
        if vid in fetched_by_id:
            v_data = fetched_by_id[vid]
            voices_to_display.append({
                "name": v_data.get("name", fallback_voices[vid]["name"]),
                "id": v_data["voice_id"],
                "desc": fallback_voices[vid]["description"],
                "preview_url": v_data.get("preview_url") or fallback_voices[vid]["preview_url"]
            })
        else:
            voices_to_display.append({
                "name": fallback_voices[vid]["name"],
                "id": fallback_voices[vid]["voice_id"],
                "desc": fallback_voices[vid]["description"],
                "preview_url": fallback_voices[vid]["preview_url"]
            })

    for idx, voice in enumerate(voices_to_display):
        is_selected = (st.session_state.selected_voice_id == voice["id"])
        
        # Wrap the columns inside a custom HTML card div
        st.markdown(
            f'<div class="voice-card-wrapper {"selected" if is_selected else ""}">',
            unsafe_allow_html=True
        )
        
        card_cols = st.columns([1.5, 1.1, 0.4])
        with card_cols[0]:
            st.markdown(
                f"""
                <div class="voice-card-text">
                    <div class="voice-card-name">{voice["name"]}</div>
                    <div class="voice-card-desc">{voice["desc"]}</div>
                </div>
                """,
                unsafe_allow_html=True
            )
            
        with card_cols[1]:
            st.audio(voice["preview_url"])
            
        with card_cols[2]:
            select_btn_label = "✅" if is_selected else "🎙️"
            if st.button(select_btn_label, key=f"select_voice_{voice['id']}", disabled=is_selected, use_container_width=True):
                st.session_state.selected_voice_id = voice["id"]
                st.session_state.selected_voice_name = voice["name"]
                st.rerun()
                
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="action-btn-container" style="margin-top: 25px;">', unsafe_allow_html=True)
    st.markdown('<div class="primary-btn-marker"></div>', unsafe_allow_html=True)
    if st.button("➡️ متابعة إلى محرر الصوت المعلق", key="btn_proceed_to_stage4", use_container_width=True):
        st.session_state.voice_selected = True
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="back-btn-container">', unsafe_allow_html=True)
    st.markdown('<div class="secondary-btn-marker"></div>', unsafe_allow_html=True)
    if st.button("⬅️ رجوع إلى تجميع الفيديو", key="reset_btn_stage3", use_container_width=True):
        st.session_state.compilation_done = False
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

elif active_stage == 4:
    comp_status = st.session_state.get("compilation_status", {})
    if comp_status.get("success"):
        st.session_state.output_path = comp_status.get("output_path")
        st.session_state.output_name = comp_status.get("output_name")
        
    st.markdown('<div class="stage4-container">', unsafe_allow_html=True)
    if "transcription_text" not in st.session_state:
        st.session_state.transcription_text = ""
    st.markdown("""
    <style>
    /* Expand parent block-container specifically for Stage 4 to fit larger player and right column */
    [data-testid="block-container"] {
        width: 760px !important;
        max-width: 95vw !important;
        height: 820px !important;
        max-height: 95vh !important;
    }

    /* Style any video player inside the left column to be a TikTok vertical frame (9:16) */
    div.stVideo, 
    div[data-testid="stVideo"], 
    div[class*="stVideo"],
    div.element-container:has(video) {
        width: 8.43cm !important;
        min-width: 8.43cm !important;
        max-width: 8.43cm !important;
        height: 15cm !important;
        min-height: 15cm !important;
        max-height: 15cm !important;
        aspect-ratio: 9 / 16 !important;
        margin: 0 auto 6px auto !important;
        border-radius: 16px !important;
        overflow: hidden !important;
        background-color: #111111 !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        box-shadow: none !important;
    }
    video {
        width: 100% !important;
        height: 100% !important;
        max-height: none !important; /* Override global max-height constraint */
        object-fit: cover !important; /* cover crops/fills landscape video to fit 9:16 portrait format */
        border-radius: 16px !important;
        background-color: #111111 !important;
    }

    /* All buttons in the right column */
    div[data-testid="stColumn"]:nth-child(2) button,
    div[data-testid="column"]:nth-child(2) button {
        height: 38px !important;
        min-height: 38px !important;
        background-color: rgba(255, 255, 255, 0.04) !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important;
        border-radius: 8px !important;
        color: #ffffff !important;
        font-size: 0.78rem !important;
        font-weight: 600 !important;
        width: 100% !important;
        box-sizing: border-box !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        transition: all 0.2s ease !important;
        margin: 0 0 8px 0 !important;
        box-shadow: none !important;
    }
    div[data-testid="stColumn"]:nth-child(2) button:hover,
    div[data-testid="column"]:nth-child(2) button:hover {
        background-color: rgba(255, 255, 255, 0.08) !important;
        border-color: rgba(255, 255, 255, 0.25) !important;
        color: #ffffff !important;
    }

    /* Force action-btn-container button to match */
    div[data-testid="stColumn"]:nth-child(2) .action-btn-container button,
    div[data-testid="column"]:nth-child(2) .action-btn-container button {
        background-color: rgba(255, 255, 255, 0.04) !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important;
        border-radius: 8px !important;
        color: #ffffff !important;
        box-shadow: none !important;
        height: 38px !important;
        min-height: 38px !important;
    }
    div[data-testid="stColumn"]:nth-child(2) .action-btn-container button:hover,
    div[data-testid="column"]:nth-child(2) .action-btn-container button:hover {
        background-color: rgba(255, 255, 255, 0.08) !important;
        border-color: rgba(255, 255, 255, 0.25) !important;
    }

    /* Text Input for TurnScribe URL */
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTextInput"],
    div[data-testid="column"]:nth-child(2) div[data-testid="stTextInput"] {
        margin-bottom: 8px !important;
    }
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTextInput"] input,
    div[data-testid="column"]:nth-child(2) div[data-testid="stTextInput"] input {
        height: 38px !important;
        min-height: 38px !important;
        background-color: rgba(255, 255, 255, 0.04) !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important;
        border-radius: 8px !important;
        color: #ffffff !important;
        font-size: 0.78rem !important;
        padding: 8px 12px 8px 38px !important;
        box-sizing: border-box !important;
    }
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTextInput"] input:focus,
    div[data-testid="column"]:nth-child(2) div[data-testid="stTextInput"] input:focus {
        border-color: var(--accent) !important;
        background-color: rgba(255, 255, 255, 0.06) !important;
    }

    /* File Uploader */
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stFileUploader"],
    div[data-testid="column"]:nth-child(2) div[data-testid="stFileUploader"] {
        background-color: transparent !important;
        border: none !important;
        padding: 0 !important;
        margin-bottom: 8px !important;
    }
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stFileUploader"] > section,
    div[data-testid="column"]:nth-child(2) div[data-testid="stFileUploader"] > section {
        min-height: 38px !important;
        height: 38px !important;
        padding: 0 12px !important;
        background-color: rgba(255, 255, 255, 0.04) !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important;
        border-radius: 8px !important;
        display: flex !important;
        flex-direction: row !important;
        align-items: center !important;
        justify-content: space-between !important;
        box-sizing: border-box !important;
    }
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stFileUploader"] > section > div,
    div[data-testid="column"]:nth-child(2) div[data-testid="stFileUploader"] > section > div {
        display: flex !important;
        flex-direction: row-reverse !important;
        align-items: center !important;
        justify-content: space-between !important;
        width: 100% !important;
        padding: 0 !important;
        gap: 10px !important;
    }
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stFileUploader"] > section svg,
    div[data-testid="column"]:nth-child(2) div[data-testid="stFileUploader"] > section svg,
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stFileUploader"] > section small,
    div[data-testid="column"]:nth-child(2) div[data-testid="stFileUploader"] > section small {
        display: none !important;
    }
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stFileUploader"] [data-testid="stFileUploadDropzoneText"],
    div[data-testid="column"]:nth-child(2) div[data-testid="stFileUploader"] [data-testid="stFileUploadDropzoneText"] {
        font-size: 0.72rem !important;
        color: #ffffff !important;
        margin: 0 !important;
        text-align: right !important;
        direction: rtl !important;
        font-weight: 600 !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
        white-space: nowrap !important;
        max-width: 180px !important;
    }
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stFileUploader"] button,
    div[data-testid="column"]:nth-child(2) div[data-testid="stFileUploader"] button {
        height: 26px !important;
        min-height: 26px !important;
        padding: 0 10px !important;
        font-size: 0.7rem !important;
        background-color: rgba(255, 255, 255, 0.08) !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important;
        color: #ffffff !important;
        border-radius: 6px !important;
        margin: 0 !important;
        width: auto !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
    }

    /* Compact styling for uploaded file widget */
    div[data-testid="stColumn"]:nth-child(2) [data-testid="stUploadedFile"],
    div[data-testid="column"]:nth-child(2) [data-testid="stUploadedFile"] {
        min-height: 38px !important;
        height: 38px !important;
        background-color: rgba(255, 255, 255, 0.04) !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important;
        border-radius: 8px !important;
        padding: 0 10px !important;
        display: flex !important;
        align-items: center !important;
        justify-content: space-between !important;
        box-sizing: border-box !important;
        margin-bottom: 8px !important;
    }

    /* Success / Info alerts */
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stNotification"],
    div[data-testid="column"]:nth-child(2) div[data-testid="stNotification"] {
        min-height: 38px !important;
        height: 38px !important;
        padding: 0 12px !important;
        display: flex !important;
        align-items: center !important;
        border-radius: 8px !important;
        margin-bottom: 8px !important;
        box-sizing: border-box !important;
        background-color: var(--accent-soft) !important;
        border: 1px solid var(--accent-border) !important;
        color: var(--accent) !important;
    }
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stNotification"] p,
    div[data-testid="column"]:nth-child(2) div[data-testid="stNotification"] p {
        font-size: 0.78rem !important;
        margin: 0 !important;
        font-weight: 600 !important;
    }

    /* Text area */
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTextArea"] textarea,
    div[data-testid="column"]:nth-child(2) div[data-testid="stTextArea"] textarea {
        background-color: rgba(255, 255, 255, 0.04) !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important;
        border-radius: 8px !important;
        color: #ffffff !important;
        font-size: 0.78rem !important;
        padding: 8px 12px !important;
        min-height: 120px !important;
        box-sizing: border-box !important;
    }
    div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTextArea"] textarea:focus,
    div[data-testid="column"]:nth-child(2) div[data-testid="stTextArea"] textarea:focus {
        border-color: var(--accent) !important;
        background-color: rgba(255, 255, 255, 0.06) !important;
    }
    </style>
    """, unsafe_allow_html=True)

    output_path = st.session_state.output_path
    output_name = st.session_state.output_name

    # Display video duration details if file exists
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        try:
            dur = get_duration(output_path)
            ok = "متوافق ✓" if abs(dur - 61.0) < 0.1 else "غير متوافق ⚠️"
            st.markdown(
                f'<div style="display:flex; justify-content:center; gap:12px; margin-bottom:12px; direction: rtl;">'
                f'<div style="background:rgba(139,26,26,0.06); border:1px solid rgba(139,26,26,0.15); border-radius:6px; padding:4px 10px; font-size:0.7rem; color:var(--accent);">'
                f'مدة الفيديو: <b>{dur:.2f} ثانية</b></div>'
                f'<div style="background:rgba(139,26,26,0.06); border:1px solid rgba(139,26,26,0.15); border-radius:6px; padding:4px 10px; font-size:0.7rem; color:var(--accent);">'
                f'الهدف (61.0 ثانية): <b>{ok}</b></div></div>',
                unsafe_allow_html=True
            )
        except Exception:
            pass

    temp_processing_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_processing")
    os.makedirs(temp_processing_dir, exist_ok=True)
    if not os.path.exists(os.path.join(temp_processing_dir, "thumb_0.jpg")):
        try:
            extract_video_thumbnails(output_path)
        except Exception:
            pass

    if "uploaded_audio_path" not in st.session_state:
        st.session_state.uploaded_audio_path = ""
    if "uploaded_audio_name" not in st.session_state:
        st.session_state.uploaded_audio_name = ""
    if "mixed_output_path" not in st.session_state:
        st.session_state.mixed_output_path = ""

    # Check for active audio
    working_audio = (
        st.session_state.uploaded_audio_path
        if st.session_state.uploaded_audio_path and os.path.exists(st.session_state.uploaded_audio_path)
        else None
    )

    if working_audio:
        try:
            audio_dur = get_duration(working_audio)
            waveform_peaks = extract_waveform_peaks(working_audio, 500)
        except Exception:
            audio_dur = 120.0
            waveform_peaks = [0.5] * 500

        # Smart Auto-Mix: delay merge if video compilation is still running
        if st.session_state.auto_mix:
            if comp_status.get("in_progress", False):
                pass # Wait, don't mix yet
            elif comp_status.get("done") and not comp_status.get("success"):
                st.session_state.auto_mix = False
                st.toast("⚠️ تعذر دمج الصوت تلقائياً بسبب فشل تجميع الفيديو.")
            elif os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                st.session_state.auto_mix = False
                with st.spinner("جاري دمج الصوت مع الفيديو تلقائياً..."):
                    try:
                        import shutil
                        temp_mixed = os.path.join(temp_processing_dir, f"mixed_{int(time.time())}.mp4")
                        mix_audio_to_video(
                            video_path=output_path,
                            audio_path=working_audio,
                            output_path=temp_mixed,
                            clips=st.session_state.clips
                        )
                        final_mixed_path = os.path.abspath("compiled_with_audio.mp4")
                        shutil.copy2(temp_mixed, final_mixed_path)
                        st.session_state.mixed_output_path = final_mixed_path
                        st.toast("✅ تم دمج الصوت مع الفيديو بنجاح!")
                    except Exception as mix_err:
                        st.error(f"فشل دمج الصوت: {mix_err}")

    # Columns setup (Left: Video preview / Progress, Right: Controls / Script editor)
    col_left, col_right = st.columns([1.4, 1.6])

    # --- LEFT COLUMN: Video / Progress bar ---
    with col_left:
        if comp_status.get("in_progress"):
            # 1. Compilation in progress
            st.markdown(
                """
                <div class="video-placeholder-container" style="width: 8.43cm; height: 15cm; border: 1px dashed rgba(255,255,255,0.1); border-radius: 16px; background: rgba(0,0,0,0.3); display: flex; flex-direction: column; justify-content: center; align-items: center; padding: 20px; direction: rtl; box-sizing: border-box; margin: 0 auto 6px auto;">
                    <div style="font-size: 1.8rem; margin-bottom: 15px;">⚙️</div>
                    <div style="color: var(--accent); font-weight: bold; font-size: 0.85rem; margin-bottom: 8px;">جاري تجميع الفيديو في الخلفية...</div>
                    <div style="color: #94a3b8; font-size: 0.75rem; text-align: center; margin-bottom: 15px; line-height: 1.4;">يمكنك تعديل السكربت والعمل في الجهة اليمنى أثناء اكتمال الفيديو.</div>
                </div>
                """,
                unsafe_allow_html=True
            )
            render_custom_progress_bar_js()
            st.markdown('<div class="primary-btn-marker"></div>', unsafe_allow_html=True)
            if st.button("🔄 تحديث الفيديو", key="btn_refresh_video_status_unified", use_container_width=True):
                st.rerun()
                
        elif comp_status.get("done") and not comp_status.get("success"):
            # 2. Compilation failed
            st.markdown(
                """
                <div class="video-placeholder-container" style="width: 8.43cm; height: 15cm; border: 1px solid rgba(239,68,68,0.2); border-radius: 16px; background: rgba(239,68,68,0.05); display: flex; flex-direction: column; justify-content: center; align-items: center; padding: 20px; direction: rtl; box-sizing: border-box; margin: 0 auto 6px auto;">
                    <div style="font-size: 1.8rem; margin-bottom: 15px;">❌</div>
                    <div style="color: #ef4444; font-weight: bold; font-size: 0.85rem; margin-bottom: 8px;">فشل تجميع الفيديو</div>
                    <div style="color: #94a3b8; font-size: 0.75rem; text-align: center; margin-bottom: 15px; line-height: 1.4;">حدث خطأ أثناء معالجة القوالب أو تجميع الفيديو النهائي.</div>
                </div>
                """,
                unsafe_allow_html=True
            )
            logs_html = "".join(comp_status.get("logs", []))
            st.markdown(f'<div class="log-box">{logs_html}</div>', unsafe_allow_html=True)
            st.error(comp_status.get("error", "Unknown error"))
            
            st.markdown('<div class="back-btn-container" style="margin-top: 15px;">', unsafe_allow_html=True)
            st.markdown('<div class="secondary-btn-marker"></div>', unsafe_allow_html=True)
            if st.button("⬅️ رجوع إلى تجميع الفيديو لإعادة المحاولة", key="retry_compile_btn_unified", use_container_width=True):
                st.session_state.compilation_done = False
                st.session_state.compilation_status = {}
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
            
        elif os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            # 3. Compilation succeeded
            st.markdown('<div style="text-align: center; color: #10b981; font-weight: bold; font-size: 0.85rem; margin-bottom: 6px;">✓ تم تجميع الفيديو بنجاح!</div>', unsafe_allow_html=True)
            if st.session_state.mixed_output_path and os.path.exists(st.session_state.mixed_output_path):
                st.video(st.session_state.mixed_output_path)
            else:
                st.video(output_path)
                
        else:
            # 4. Video not compiled yet / not started
            st.markdown(
                """
                <div class="video-placeholder-container" style="width: 8.43cm; height: 15cm; border: 1px dashed rgba(255,255,255,0.1); border-radius: 16px; background: rgba(0,0,0,0.3); display: flex; flex-direction: column; justify-content: center; align-items: center; padding: 20px; direction: rtl; box-sizing: border-box; margin: 0 auto 6px auto;">
                    <div style="font-size: 1.8rem; margin-bottom: 15px;">🔍</div>
                    <div style="color: #94a3b8; font-size: 0.8rem;">الفيديو غير متوفر حالياً.</div>
                </div>
                """,
                unsafe_allow_html=True
            )

    # --- RIGHT COLUMN: Controls / Script / Audio ---
    with col_right:
        # Expander for OpenRouter Key (AI Optimizer)
        with st.expander("🤖 مفتاح OpenRouter API (كلود)"):
            or_key = st.session_state.get("user_openrouter_key", "")
            if or_key:
                masked_or = or_key[:8] + "•" * 10 + or_key[-6:]
                st.markdown(
                    f"""
                    <div style="background-color: rgba(16, 185, 129, 0.06); border: 1px solid rgba(16, 185, 129, 0.15); border-radius: 8px; padding: 6px 12px; margin-bottom: 12px; text-align: center; direction: rtl;">
                        <span style="color: #10b981; font-weight: 700; font-size: 0.75rem;">🤖 OpenRouter: ✅ نشط — {masked_or}</span>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            else:
                st.markdown(
                    f"""
                    <div style="background-color: rgba(239, 68, 68, 0.06); border: 1px solid rgba(239, 68, 68, 0.15); border-radius: 8px; padding: 6px 12px; margin-bottom: 12px; text-align: center; direction: rtl;">
                        <span style="color: #f87171; font-weight: 700; font-size: 0.75rem;">🤖 OpenRouter: ⚠️ لا يوجد مفتاح — زر تحسين السكربت معطل</span>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

            def _save_or_key_s4():
                raw = st.session_state.get("or_key_input_s4", "").strip()
                if not raw:
                    return
                if not raw.startswith("sk-or-"):
                    st.toast("⚠️ مفتاح OpenRouter يجب أن يبدأ بـ sk-or-")
                    return
                st.session_state.user_openrouter_key = raw
                _persist_api_key("openrouter", raw)
                st.session_state["or_key_input_s4"] = ""
                st.toast("✅ تم حفظ مفتاح OpenRouter بنجاح!")

            def _clear_or_key_s4():
                st.session_state.user_openrouter_key = ""
                _persist_api_key("openrouter", "")
                st.toast("🗑️ تم مسح مفتاح OpenRouter")

            col_input, col_btn = st.columns([3.5, 1.2])
            with col_input:
                st.text_input(
                    "مفتاح OpenRouter",
                    value="",
                    placeholder="ألصق مفتاح sk-or-... هنا",
                    type="password",
                    key="or_key_input_s4",
                    label_visibility="collapsed",
                    on_change=_save_or_key_s4,
                )
            with col_btn:
                st.button(
                    "🗑️ مسح",
                    key="btn_clear_or_s4",
                    on_click=_clear_or_key_s4,
                    use_container_width=True
                )

        # Expander for ElevenLabs Key (Voiceover)
        with st.expander("🎙️ مفتاح ElevenLabs API"):
            el_key = st.session_state.get("user_elevenlabs_key", "")
            if el_key:
                masked_el = el_key[:6] + "•" * 10 + el_key[-4:]
                st.markdown(
                    f"""
                    <div style="background-color: rgba(16, 185, 129, 0.06); border: 1px solid rgba(16, 185, 129, 0.15); border-radius: 8px; padding: 6px 12px; margin-bottom: 12px; text-align: center; direction: rtl;">
                        <span style="color: #10b981; font-weight: 700; font-size: 0.75rem;">🎙️ ElevenLabs: ✅ نشط — {masked_el}</span>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            else:
                st.markdown(
                    f"""
                    <div style="background-color: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px; padding: 6px 12px; margin-bottom: 12px; text-align: center; direction: rtl;">
                        <span style="color: #a1a1aa; font-weight: 700; font-size: 0.75rem;">🎙️ ElevenLabs: يستخدم المفتاح الافتراضي (حصته قد تنفذ)</span>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

            def _save_el_key_s4():
                raw = st.session_state.get("el_key_input_s4", "").strip()
                if not raw:
                    return
                st.session_state.user_elevenlabs_key = raw
                _persist_api_key("elevenlabs", raw)
                st.session_state["el_key_input_s4"] = ""
                st.toast("✅ تم حفظ مفتاح ElevenLabs بنجاح!")

            def _clear_el_key_s4():
                st.session_state.user_elevenlabs_key = ""
                _persist_api_key("elevenlabs", "")
                st.toast("🗑️ تم مسح مفتاح ElevenLabs")

            col_input_el, col_btn_el = st.columns([3.5, 1.2])
            with col_input_el:
                st.text_input(
                    "مفتاح ElevenLabs",
                    value="",
                    placeholder="ألصق مفتاح ElevenLabs هنا...",
                    type="password",
                    key="el_key_input_s4",
                    label_visibility="collapsed",
                    on_change=_save_el_key_s4,
                )
            with col_btn_el:
                st.button(
                    "🗑️ مسح",
                    key="btn_clear_el_s4",
                    on_click=_clear_el_key_s4,
                    use_container_width=True
                )

        # 1. Audio Upload Widget (Always present at top)
        st.markdown(f"<div style='direction: rtl; text-align: right; font-size: 12px; color: #94a3b8; font-weight: 600; margin-bottom: 4px;'>رفع ملف صوتي أو فيديو:</div>", unsafe_allow_html=True)
        if "uploader_version" not in st.session_state:
            st.session_state.uploader_version = 0
        audio_file = st.file_uploader(
            "audio_upload",
            type=["mp3", "wav", "m4a", "mp4", "mov", "mkv"],
            key=f"audio_uploader_{st.session_state.uploader_version}",
            label_visibility="collapsed"
        )

        if audio_file is not None:
            temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_processing")
            os.makedirs(temp_dir, exist_ok=True)
            suffix = os.path.splitext(audio_file.name)[1].lower()
            timestamp = int(time.time())
            uploaded_temp = os.path.join(temp_dir, f"audio_uploaded_{timestamp}{suffix}")
            with open(uploaded_temp, "wb") as f:
                f.write(audio_file.getbuffer())

            is_video = suffix in [".mp4", ".mov", ".mkv"]
            audio_temp = os.path.join(temp_dir, f"audio_extracted_{timestamp}.mp3")
            if is_video:
                st.info("جاري استخراج الصوت...")
                try:
                    extract_audio(uploaded_temp, audio_temp)
                    st.success("تم استخراج الصوت!")
                    working_audio_new = audio_temp
                except Exception as audio_err:
                    st.error(f"فشل استخراج الصوت: {audio_err}")
                    working_audio_new = None
            else:
                working_audio_new = uploaded_temp

            if working_audio_new:
                st.session_state.uploaded_audio_path = working_audio_new
                st.session_state.uploaded_audio_name = audio_file.name
                st.session_state.auto_mix = True
                try:
                    dur_val = get_duration(working_audio_new)
                except Exception:
                    dur_val = 120.0
                st.session_state.clips = [{"trim_start": 0.0, "trim_end": dur_val, "delay_sec": 0.0, "volume": 1.0}]
                st.rerun()

        # Display Audio info if working audio is loaded
        if working_audio:
            st.markdown(
                f"""
                <div style='display: flex; justify-content: flex-end; margin-top: 4px; margin-bottom: 8px; direction: rtl;'>
                    <div style='background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 6px; padding: 4px 10px; font-size: 0.75rem; color: #e2e8f0; font-weight: 500;'>
                        🎵 <b>{st.session_state.get('uploaded_audio_name', 'الملف الصوتي')}</b> <span style='color: #94a3b8; font-size: 0.7rem;'>({audio_dur:.2f} ثانية)</span>
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
            
            # Show Auto Mix status notice if it is waiting for the video
            if st.session_state.auto_mix and comp_status.get("in_progress", False):
                st.markdown(
                    """
                    <div style="background-color: rgba(16, 185, 129, 0.05); border: 1px dashed rgba(16, 185, 129, 0.2); border-radius: 8px; padding: 8px 10px; font-size: 0.75rem; color: #10b981; text-align: center; margin-bottom: 12px; direction: rtl;">
                        ⏳ الصوت جاهز! سيتم دمجه تلقائياً بمجرد اكتمال تجميع الفيديو في اليسار.
                    </div>
                    """,
                    unsafe_allow_html=True
                )

        st.markdown("<hr style='margin: 12px 0; border-color: rgba(255,255,255,0.06);'>", unsafe_allow_html=True)

        # 2. TurnScribe Link Extractor
        st.markdown(f"<div style='direction: rtl; text-align: right; font-size: 12px; color: #94a3b8; font-weight: 600; margin-bottom: 4px;'>استخراج النص من فيديو (TurnScribe API):</div>", unsafe_allow_html=True)
        turnscribe_url_na = st.text_input(
            "رابط الفيديو للاستخراج",
            value="",
            placeholder="ألصق رابط فيديو تيك توك أو يوتيوب هنا...",
            key="turnscribe_video_url_no_audio",
            label_visibility="collapsed"
        )
        
        st.markdown('<div class="secondary-btn-marker"></div>', unsafe_allow_html=True)
        if st.button("🔮 استخراج النص من الفيديو", key="btn_transcribe_turnscribe_no_audio", use_container_width=True):
            if turnscribe_url_na.strip():
                with st.spinner("جاري استخراج السكربت من TurnScribe..."):
                    api_key = "YUqcVbpG.zmiVf35jUdJevmCPdCCls2YnQBPrIeeo"
                    text_output = get_turnscribe_transcription(turnscribe_url_na.strip(), api_key)
                    if text_output.startswith("Error"):
                        st.error(text_output)
                    else:
                        st.session_state.transcription_text = text_output
                        st.session_state.script_area_version = st.session_state.get("script_area_version", 0) + 1
                        st.toast("✅ تم استخراج السكربت بنجاح!")
                        st.rerun()
            else:
                st.warning("الرجاء إدخال رابط فيديو صالح أولاً.")

        # Whisper Transcription Button (Only if audio uploaded and Groq key present)
        groq_key = st.session_state.get("user_groq_key", "").strip()
        if groq_key and working_audio:
            st.markdown("<hr style='margin: 8px 0; border-color: rgba(255,255,255,0.06);'>", unsafe_allow_html=True)
            st.markdown('<div class="primary-btn-marker"></div>', unsafe_allow_html=True)
            if st.button("🎙️ تفريغ الصوت المرفوع بدقة (Whisper)", key="btn_transcribe_whisper", use_container_width=True):
                with st.spinner("جاري تفريغ الصوت باستخدام Whisper..."):
                    try:
                        whisper_text, _ = transcribe_audio_with_groq(working_audio, groq_key)
                        st.session_state.transcription_text = whisper_text
                        st.session_state.script_area_version = st.session_state.get("script_area_version", 0) + 1
                        st.toast("✅ تم تفريغ الصوت بنجاح!")
                        st.rerun()
                    except Exception as whisper_err:
                        st.error(f"فشل تفريغ الصوت: {whisper_err}")

        # Check if optimized script is still intact
        is_optimized = st.session_state.get("script_is_optimized", False) and (st.session_state.transcription_text == st.session_state.get("last_optimized_script", ""))
        
        # 3. Script Text Area
        st.markdown(f"<div style='direction: rtl; text-align: right; font-size: 12px; color: #94a3b8; font-weight: 600; margin-top: 8px; margin-bottom: 4px;'>السكربت المستخرج (قابل للتعديل):</div>", unsafe_allow_html=True)
        
        if is_optimized:
            st.markdown(
                """
                <style>
                div[data-testid="stTextArea"] textarea {
                    border: 2px solid #10b981 !important;
                    box-shadow: 0 0 10px rgba(16, 185, 129, 0.35) !important;
                }
                </style>
                <div style='display: flex; justify-content: flex-start; margin-bottom: 6px; direction: rtl;'>
                    <div style='background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 6px; padding: 4px 10px; font-size: 0.75rem; color: #10b981; font-weight: 600; display: inline-flex; align-items: center; gap: 6px;'>
                        <span>✓ تم تحسين السكربت بنجاح بالذكاء الاصطناعي</span>
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
            
        _na_key = f"script_na_{st.session_state.get('script_area_version', 0)}"
        st.session_state.transcription_text = st.text_area(
            "السكربت",
            value=st.session_state.transcription_text,
            height=130,
            key=_na_key,
            label_visibility="collapsed"
        )
        
        # 4. Word Count
        script_text_na = st.session_state.transcription_text.strip()
        if script_text_na:
            words_count_na = count_words(script_text_na)
            is_ok_na = 177 <= words_count_na <= 185
            wc_color_na = "#10b981" if is_ok_na else "#ef4444"
            bg_color = "rgba(16, 185, 129, 0.06)" if is_ok_na else "rgba(239, 68, 68, 0.06)"
            border_color = "rgba(16, 185, 129, 0.15)" if is_ok_na else "rgba(239, 68, 68, 0.15)"
            st.markdown(
                f"""
                <div style='display: flex; justify-content: flex-end; margin-top: 6px; margin-bottom: 8px; direction: rtl;'>
                    <div style='background: {bg_color}; border: 1px solid {border_color}; border-radius: 6px; padding: 4px 10px; font-size: 0.75rem; color: {wc_color_na}; font-weight: 600; display: inline-flex; align-items: center; gap: 6px;'>
                        <span>📝 عدد الكلمات: <b>{words_count_na}</b> كلمة</span>
                        <span style='opacity: 0.85; font-size: 0.7rem;'>{"(متوافق ✓)" if is_ok_na else "(المدى المطلوب: 177 - 185)"}</span>
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
        
        # 5. Optimize & Generate / Mix buttons
        btn_col1_na, btn_col2_na = st.columns(2)
        with btn_col1_na:
            st.markdown('<div class="secondary-btn-marker"></div>', unsafe_allow_html=True)
            if st.button("✨ تحسين السكربت", key="btn_optimize_script_no_audio", use_container_width=True):
                current_script_na = st.session_state.transcription_text.strip()
                if current_script_na:
                    with st.spinner("جاري تحسين السكربت بالذكاء الاصطناعي..."):
                        custom_or_key = st.session_state.get("user_openrouter_key", "")
                        optimized = run_openrouter_optimization(current_script_na, custom_api_key=custom_or_key)
                        if optimized.startswith("Error"):
                            st.error(optimized)
                        else:
                            st.session_state.transcription_text = optimized
                            st.session_state.last_optimized_script = optimized
                            st.session_state.script_is_optimized = True
                            st.session_state.script_area_version = st.session_state.get("script_area_version", 0) + 1
                            st.toast("✅ تم تحسين السكربت بنجاح!")
                            st.rerun()
                else:
                    st.warning("الرجاء إدخال أو استخراج السكربت أولاً قبل تحسينه.")
                    
        with btn_col2_na:
            if working_audio:
                # Flow with active audio: "Merge Audio and Subs" button
                disable_merge = comp_status.get("in_progress", False) or not (os.path.exists(output_path) and os.path.getsize(output_path) > 0)
                if disable_merge and comp_status.get("in_progress", False):
                    st.markdown(
                        """
                        <div style="background-color: rgba(245, 158, 11, 0.05); border: 1px dashed rgba(245, 158, 11, 0.2); border-radius: 8px; padding: 6px; font-size: 0.72rem; color: #fbbf24; text-align: center;">
                            ⚠️ يرجى انتظار تجميع الفيديو في اليسار.
                        </div>
                        """,
                        unsafe_allow_html=True
                    )
                st.markdown('<div class="primary-btn-marker"></div>', unsafe_allow_html=True)
                if st.button("🎬 دمج الصوت مع الفيديو", key="btn_merge_audio_and_subs", disabled=disable_merge, use_container_width=True):
                    with st.spinner("جاري دمج الصوت مع الفيديو..."):
                        try:
                            temp_mixed = os.path.join(temp_processing_dir, f"mixed_manual_{int(time.time())}.mp4")
                            mix_audio_to_video(
                                video_path=output_path,
                                audio_path=working_audio,
                                output_path=temp_mixed,
                                clips=[{"trim_start": 0.0, "trim_end": audio_dur, "delay_sec": 0.0, "volume": 1.0}]
                            )
                            final_mixed_path = os.path.abspath("compiled_with_audio.mp4")
                            import shutil
                            shutil.copy2(temp_mixed, final_mixed_path)
                            st.session_state.mixed_output_path = final_mixed_path
                            st.session_state.voiceover_generated = True
                            st.success("تم دمج الصوت بنجاح!")
                            st.rerun()
                        except Exception as mix_err:
                            st.error(f"فشل دمج الصوت مع الفيديو: {mix_err}")
            else:
                # Flow without audio: "Generate Voiceover" button (Always enabled!)
                st.markdown('<div class="primary-btn-marker"></div>', unsafe_allow_html=True)
                if st.button("🔊 توليد التعليق الصوتي (ElevenLabs)", key="btn_generate_voiceover_no_audio", use_container_width=True):
                    script_text = st.session_state.transcription_text.strip()
                    if not script_text:
                        st.warning("الرجاء إدخال السكربت أولاً.")
                    else:
                        with st.spinner("جاري توليد التعليق الصوتي من ElevenLabs..."):
                            audio_out = os.path.join(temp_processing_dir, f"elevenlabs_voiceover_{int(time.time())}.mp3")
                            voice_id = st.session_state.get("selected_voice_id", "5Qfm4RqcAer0xoyWtoHC")
                            api_key = st.session_state.get("user_elevenlabs_key", "").strip() or "a6bda3b0ff51f175dd88330b718acac592a0dbd6a9ac5135368a743179586bf3"
                            
                            success, err_msg, _ = generate_elevenlabs_audio(
                                text=script_text,
                                voice_id=voice_id,
                                api_key=api_key,
                                output_path=audio_out,
                                with_timestamps=False
                            )
                        
                        if success:
                            st.session_state.uploaded_audio_path = audio_out
                            st.session_state.uploaded_audio_name = "تعليق صوتي ElevenLabs"
                            st.session_state.auto_mix = True
                            try:
                                v_dur = get_duration(audio_out)
                            except Exception:
                                v_dur = 61.0
                            st.session_state.clips = [{"trim_start": 0.0, "trim_end": v_dur, "delay_sec": 0.0, "volume": 1.0}]
                            st.success("تم توليد التعليق الصوتي بنجاح!")
                            st.rerun()
                        else:
                            st.error(f"فشل توليد الصوت: {err_msg}")

        # 6. Download Buttons (Merged output with audio, or raw video if no audio)
        if working_audio:
            if st.session_state.mixed_output_path and os.path.exists(st.session_state.mixed_output_path):
                st.markdown("<hr style='margin: 12px 0; border-color: rgba(255,255,255,0.06);'>", unsafe_allow_html=True)
                st.markdown("<div style='direction: rtl; text-align: right; font-size: 13px; color: #ffffff; font-weight: 600; margin-bottom: 4px;'>تحميل الفيديو مع الصوت:</div>", unsafe_allow_html=True)
                try:
                    with open(st.session_state.mixed_output_path, "rb") as _f:
                        _mixed_bytes = _f.read()
                    st.markdown('<div class="primary-btn-marker"></div>', unsafe_allow_html=True)
                    st.download_button(
                        label="تحميل الفيديو النهائي مع الصوت (1080p) 📥",
                        data=_mixed_bytes,
                        file_name="compiled_with_audio.mp4",
                        mime="video/mp4",
                        key="dl_with_audio",
                        on_click=cleanup_temporary_files,
                        kwargs={"force": True, "clean_root": False}
                    )
                except Exception as _dl_err:
                    st.error(f"خطأ أثناء التحميل: {_dl_err}")
        else:
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                st.markdown("<hr style='margin: 12px 0; border-color: rgba(255,255,255,0.06);'>", unsafe_allow_html=True)
                try:
                    with open(output_path, "rb") as _f:
                        _video_bytes = _f.read()
                    st.markdown('<div class="secondary-btn-marker"></div>', unsafe_allow_html=True)
                    st.download_button(
                        label="تحميل الفيديو (بدون صوت) 📥",
                        data=_video_bytes,
                        file_name=output_name,
                        mime="video/mp4",
                        key="dl_no_audio",
                        on_click=cleanup_temporary_files,
                        kwargs={"force": True, "clean_root": False}
                    )
                except Exception as _dl_err:
                    st.error(f"خطأ أثناء التحميل: {_dl_err}")

        # 7. Remove Audio button (If working audio exists)
        if working_audio:
            st.markdown('<div class="secondary-btn-marker"></div>', unsafe_allow_html=True)
            if st.button("❌ إزالة الصوت / رفع ملف آخر", key="remove_audio_btn"):
                if st.session_state.uploaded_audio_path and os.path.exists(st.session_state.uploaded_audio_path):
                    try:
                        os.remove(st.session_state.uploaded_audio_path)
                    except Exception:
                        pass
                st.session_state.uploaded_audio_path = ""
                st.session_state.uploaded_audio_name = ""
                st.session_state.mixed_output_path = ""
                st.session_state.uploader_version = st.session_state.get("uploader_version", 0) + 1
                st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)

elif active_stage == 5:
    st.markdown("""
    <style>
    /* Expand parent block-container specifically for Stage 5 to fit larger centered player */
    [data-testid="block-container"] {
        width: 760px !important;
        max-width: 95vw !important;
        height: 820px !important;
        max-height: 95vh !important;
    }

    .success-title {
        text-align: center;
        margin-bottom: 16px;
        margin-top: 10px;
    }
    .success-title h3 {
        font-family: 'Outfit', sans-serif;
        font-size: 1.35rem;
        font-weight: 800;
        color: var(--accent);
        margin: 0;
        line-height: 1.3;
        direction: rtl;
    }
    /* Portrait compact video preview */
    div.stVideo, 
    div[data-testid="stVideo"], 
    div[class*="stVideo"],
    div.element-container:has(video) {
        width: 8.43cm !important;
        min-width: 8.43cm !important;
        max-width: 8.43cm !important;
        height: 15cm !important;
        min-height: 15cm !important;
        max-height: 15cm !important;
        aspect-ratio: 9 / 16 !important;
        margin: 0 auto 12px auto !important;
        border-radius: 16px !important;
        overflow: hidden !important;
        background-color: #111111 !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        box-shadow: none !important;
    }
    video {
        width: 100% !important;
        height: 100% !important;
        max-height: none !important; /* Override global max-height constraint */
        object-fit: cover !important;
        border-radius: 16px !important;
        background-color: #111111 !important;
    }
    </style>
    """, unsafe_allow_html=True)

    st.markdown(
        """
        <div class="success-title">
            <h3>🎉 الفيديو النهائي جاهز للتحميل!</h3>
        </div>
        """,
        unsafe_allow_html=True
    )

    mixed_path = st.session_state.get("mixed_output_path", "")
    if mixed_path and os.path.exists(mixed_path):
        st.video(mixed_path)

        try:
            with open(mixed_path, "rb") as f:
                video_bytes = f.read()
            
            st.markdown('<div class="primary-btn-marker"></div>', unsafe_allow_html=True)
            st.download_button(
                label="📥 تحميل الفيديو النهائي (مع الصوت)",
                data=video_bytes,
                file_name="final_output_voiceover.mp4",
                mime="video/mp4",
                key="dl_final_stage5",
                on_click=cleanup_temporary_files,
                kwargs={"force": True, "clean_root": False}
            )
        except Exception as err:
            st.error(f"خطأ أثناء قراءة ملف الفيديو: {err}")
    else:
        st.error("لم يتم العثور على الفيديو المولد. يرجى العودة للخطوة السابقة وتوليده.")

    # Back buttons
    st.markdown('<div class="back-btn-container" style="margin-top: 14px;">', unsafe_allow_html=True)
    st.markdown('<div class="secondary-btn-marker"></div>', unsafe_allow_html=True)
    if st.button("⬅️ تعديل السكربت وإعادة التوليد", key="back_to_stage4_btn"):
        st.session_state.voiceover_generated = False
        st.rerun()
    
    st.markdown('<div class="secondary-btn-marker"></div>', unsafe_allow_html=True)
    if st.button("🎙️ تغيير المعلق الصوتي (ElevenLabs)", key="change_voice_btn_stage5"):
        st.session_state.voiceover_generated = False
        st.session_state.voice_selected = False
        st.rerun()

    st.markdown('<div class="secondary-btn-marker"></div>', unsafe_allow_html=True)
    if st.button("🔄 بدء مشروع جديد من البداية", key="reset_entire_app_btn"):
        cleanup_temporary_files(force=True, clean_root=True)
        st.session_state.templates_built = False
        st.session_state.compilation_done = False
        st.session_state.voice_selected = False
        st.session_state.voiceover_generated = False
        st.session_state.template_paths = []
        st.session_state.built_urls = []
        st.session_state.uploaded_audio_path = ""
        st.session_state.uploaded_audio_name = ""
        st.session_state.mixed_output_path = ""
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)
