# -*- coding: utf-8 -*-
import sys
import subprocess
import json
import re
import os
import shutil
import ssl
import tempfile
from typing import List, Optional, Tuple
from urllib.request import urlopen
from urllib.error import URLError

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLineEdit, QPushButton, QTextEdit, QLabel, QComboBox,
    QFileDialog, QProgressBar, QMessageBox, QDesktopWidget, QFrame, QScrollArea
)
from PyQt5.QtCore import (
    pyqtSignal, QObject, Qt, QThread, QTimer, QRectF, QUrl, QPointF, QSettings,
    QStandardPaths
)
from PyQt5.QtGui import (
    QPixmap, QFont, QColor, QImage, QPainter, QPainterPath,
    QDesktopServices, QCursor, QPen
)

# --- Constants ---
# Low minimum bounds so users on any monitor or laptop scaling can resize freely
MIN_WINDOW_WIDTH = 640
MIN_WINDOW_HEIGHT = 500
DEFAULT_WINDOW_WIDTH = 740
DEFAULT_WINDOW_HEIGHT = 760
APP_NAME = "OmniStream Studio | by Angelo Golloberda"
SETTINGS_ORG = "AngeloGolloberda"
SETTINGS_APP = "OmniStreamStudio"

# --- Ensure Local / Bundled Binaries (yt-dlp, ffmpeg) are discovered in PATH ---
_app_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
_exe_dir = os.path.dirname(sys.executable)
_scripts_dir = os.path.join(_exe_dir, "Scripts")


def _setup_binary_environment():
    """Locates yt-dlp and ffmpeg in application / executable folders and injects them into PATH."""
    found_dirs = []
    ffmpeg_dir = None

    search_roots = [_exe_dir, _scripts_dir, _app_dir]
    for root_dir in search_roots:
        if not root_dir or not os.path.isdir(root_dir):
            continue
        if root_dir not in found_dirs:
            found_dirs.append(root_dir)
        for dirpath, dirnames, filenames in os.walk(root_dir):
            flist_lower = [f.lower() for f in filenames]
            if "ffmpeg.exe" in flist_lower or "yt-dlp.exe" in flist_lower:
                if dirpath not in found_dirs:
                    found_dirs.append(dirpath)
                if "ffmpeg.exe" in flist_lower and not ffmpeg_dir:
                    ffmpeg_dir = dirpath
            rel = os.path.relpath(dirpath, root_dir)
            if rel.count(os.sep) >= 3:
                dirnames.clear()

    for d in reversed(found_dirs):
        if d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

    return ffmpeg_dir


FFMPEG_LOCATION = _setup_binary_environment()


def get_ytdlp_executable():
    """Returns the absolute path to the local or system yt-dlp binary (prioritizes standalone binaries)."""
    # 1. Search PATH for a standalone release binary (e.g. WinGet, Scoop, or manual install)
    raw_path = os.environ.get("PATH", "")
    found_binaries = []
    for folder in raw_path.split(os.pathsep):
        if not folder or not os.path.isdir(folder):
            continue
        candidate = os.path.join(folder, "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
        if os.path.isfile(candidate) and candidate not in found_binaries:
            found_binaries.append(candidate)

    # Prioritize standalone release binaries (outside Python virtual environments/Scripts)
    standalone = [
        b for b in found_binaries
        if "scripts" not in b.lower() and "site-packages" not in b.lower() and ".venv" not in b.lower()
    ]
    if standalone:
        return standalone[0]

    # 2. Local bundled binary in application directory or next to script
    local_candidates = [
        os.path.join(_app_dir, "yt-dlp.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "yt-dlp.exe"),
        os.path.join(_exe_dir, "yt-dlp.exe"),
    ]
    for path in local_candidates:
        if os.path.isfile(path) and "scripts" not in path.lower() and ".venv" not in path.lower():
            return path

    # 3. Fallback to venv / scripts if nothing else is available
    if found_binaries:
        return found_binaries[0]

    return "yt-dlp"


def get_ffmpeg_executable():
    """Returns the absolute path to local or system ffmpeg binary, or None if unavailable."""
    candidates = []
    if FFMPEG_LOCATION:
        candidates.append(os.path.join(FFMPEG_LOCATION, "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"))
    candidates.extend([
        os.path.join(_exe_dir, "ffmpeg.exe"),
        os.path.join(_scripts_dir, "ffmpeg.exe"),
        os.path.join(_app_dir, "ffmpeg.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe"),
    ])
    for path in candidates:
        if path and os.path.isfile(path):
            return path

    return shutil.which("ffmpeg")


def has_ffmpeg() -> bool:
    """Checks whether FFmpeg is available for stream merging and post-processing."""
    return bool(get_ffmpeg_executable())


def terminate_process_tree(process):
    """Cleanly terminates a subprocess and all child processes (e.g. spawned ffmpeg)."""
    if process is None:
        return
    try:
        if process.poll() is not None:
            return
    except Exception:
        return

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=5
            )
        else:
            process.terminate()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


# --- Helper Functions ---
def format_bytes(size_bytes):
    """Converts bytes into a human-readable string (KB, MB, GB)."""
    if size_bytes is None:
        return "N/A"
    if size_bytes == 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = 0
    size_float = float(size_bytes)
    while size_float >= 1024.0 and i < len(size_name) - 1:
        size_float /= 1024.0
        i += 1
    return f"{size_float:.1f} {size_name[i]}" if i > 0 else f"{int(size_float)} B"


def format_duration(seconds):
    """Converts seconds into HH:MM:SS or MM:SS format."""
    if not seconds:
        return "--:--"
    try:
        sec = int(seconds)
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
    except (ValueError, TypeError):
        return "--:--"


def format_views(views):
    """Converts view count into human-friendly format (e.g. 1.4M views)."""
    if not views:
        return ""
    try:
        v = int(views)
        if v >= 1_000_000_000:
            return f"{v / 1_000_000_000:.1f}B views"
        if v >= 1_000_000:
            return f"{v / 1_000_000:.1f}M views"
        if v >= 1_000:
            return f"{v / 1_000:.1f}K views"
        return f"{v} views"
    except (ValueError, TypeError):
        return ""


def sanitize_filename(name: str) -> str:
    """Sanitizes a custom filename/folder title for safe cross-platform filesystem paths."""
    if not name:
        return ""
    # Strip extension if the user accidentally typed .mp4/.mp3 at the end
    cleaned = re.sub(r"\.(mp4|mkv|webm|mp3|m4a|opus|wav|flac)$", "", name.strip(), flags=re.IGNORECASE)
    # Replace illegal Windows filename characters and control chars
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    # Guard against reserved Windows device names
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if cleaned.upper() in reserved:
        cleaned = f"_{cleaned}_"
    return cleaned[:180]


def parse_timestamp_seconds(ts_text: str):
    """
    Parses flexible timestamp strings ('90', '01:30', '1:05:20', '00:10.5') into seconds.
    Returns float seconds, or None if invalid.
    """
    raw = (ts_text or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) > 3:
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 for n in nums):
        return None
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        m, s = nums
        if s >= 60:
            return None
        return m * 60.0 + s
    h, m, s = nums
    if m >= 60 or s >= 60:
        return None
    return h * 3600.0 + m * 60.0 + s


def seconds_to_ytdlp_ts(seconds: float) -> str:
    """Formats seconds into HH:MM:SS(.ms) string for yt-dlp --download-sections."""
    total_int = int(seconds)
    frac = seconds - total_int
    h = total_int // 3600
    m = (total_int % 3600) // 60
    s = total_int % 60
    base = f"{h:02d}:{m:02d}:{s:02d}"
    if frac >= 0.01:
        return f"{base}.{int(round(frac * 100)):02d}"
    return base


def resolve_subtitle_langs(video_info: dict) -> str:
    """
    Builds a safe --sub-langs selector using exact language codes rather than '.*' wildcards
    (which match YouTube 'en-de'/'en-fr' auto-translations and trigger HTTP 429 rate limits).
    """
    codes: List[str] = []

    def _add(code: str):
        if code and code not in codes:
            codes.append(code)

    orig_lang = (video_info or {}).get("language")
    if isinstance(orig_lang, str) and orig_lang.strip():
        clean_orig = orig_lang.strip()
        base = clean_orig.split("-")[0].lower()
        if re.match(r"^[A-Za-z0-9_-]+$", clean_orig):
            _add(f"{base}-orig")
            _add(clean_orig)
        if re.match(r"^[a-z]{2,3}$", base):
            _add(base)

    # Include manual subtitle languages if reasonably few (e.g. Panopto / Webex / university lectures)
    manual_subs = (video_info or {}).get("subtitles")
    if isinstance(manual_subs, dict):
        valid_keys = [k for k in manual_subs.keys() if k and k != "live_chat"]
        if 0 < len(valid_keys) <= 5:
            for k in valid_keys:
                if re.match(r"^[A-Za-z0-9_-]+$", k):
                    _add(k)

    for en_code in ("en-orig", "en", "en-US", "en-GB"):
        _add(en_code)
    _add("-live_chat")
    return ",".join(codes)


# --- Signals ---
class OutputSignal(QObject):
    output_received = pyqtSignal(str)
    progress_updated = pyqtSignal(int)
    stats_updated = pyqtSignal(str, str, str)  # speed, eta, size
    stage_updated = pyqtSignal(str)            # post-processing status text
    file_produced = pyqtSignal(str)            # final output file path
    download_finished = pyqtSignal(bool, str)


class MetadataSignal(QObject):
    metadata_ready = pyqtSignal(int, dict)     # request_id, metadata
    error_occurred = pyqtSignal(int, str)      # request_id, error message


class ThumbnailSignal(QObject):
    thumbnail_ready = pyqtSignal(int, bytes)   # request_id, image bytes
    error_occurred = pyqtSignal(int, str)      # request_id, error message


# --- Threads ---
class ThumbnailLoaderThread(QThread):
    """Fetches thumbnail in background with request-ID tagging to prevent stale UI updates."""
    def __init__(self, request_id: int, url: str, signal: ThumbnailSignal):
        super().__init__()
        self.request_id = request_id
        self.url = url
        self.signal = signal
        self._aborted = False

    def abort(self):
        self._aborted = True

    def run(self):
        try:
            try:
                ctx = ssl.create_default_context()
                with urlopen(self.url, context=ctx, timeout=12) as response:
                    if self._aborted:
                        return
                    data = response.read()
            except URLError:
                # Fallback for university intranet portals with self-signed certificates
                unverified_ctx = ssl._create_unverified_context()
                with urlopen(self.url, context=unverified_ctx, timeout=12) as response:
                    if self._aborted:
                        return
                    data = response.read()
            if not self._aborted:
                self.signal.thumbnail_ready.emit(self.request_id, data)
        except Exception as e:
            if not self._aborted:
                self.signal.error_occurred.emit(self.request_id, str(e))


AUXILIARY_DOWNLOAD_EXTS = (
    ".vtt", ".srt", ".ass", ".lrc", ".ttml",
    ".srv1", ".srv2", ".srv3", ".json3",
    ".webp", ".jpg", ".jpeg", ".png", ".description"
)


class DownloadThread(QThread):
    """Handles the yt-dlp download process in a separate thread with speed, lecture & playlist support."""
    def __init__(
        self,
        url: str,
        quality: str,
        filetype: str,
        output_signal: OutputSignal,
        output_path: str,
        cookie_args: Optional[List[str]] = None,
        is_playlist: bool = False,
        playlist_end: Optional[int] = None,
        playlist_items: Optional[str] = None,
        concurrent_fragments: int = 4,
        embed_metadata: bool = True,
        download_subs: bool = False,
        sub_langs: str = "en-orig,en,en-US,en-GB,-live_chat",
        embed_chapters: bool = True,
        sponsorblock: bool = False,
        clip_section: Optional[str] = None,
        custom_name: str = ""
    ):
        super().__init__()
        self.url = url
        self.quality = quality
        self.filetype = filetype
        self.output_signal = output_signal
        self.output_path = output_path
        self.cookie_args = cookie_args or []
        self.is_playlist = is_playlist
        self.playlist_end = playlist_end
        self.playlist_items = playlist_items
        self.concurrent_fragments = max(1, int(concurrent_fragments or 4))
        self.embed_metadata = embed_metadata
        self.download_subs = download_subs
        self.sub_langs = sub_langs or "en-orig,en,en-US,en-GB,-live_chat"
        self.embed_chapters = embed_chapters
        self.sponsorblock = sponsorblock
        self.clip_section = clip_section
        self.custom_name = sanitize_filename(custom_name)

        self.seen_items = 0
        self.downloaded_count = 0
        self.playlist_finished = False
        self._is_running = True
        self._process = None
        self._detected_filepath = ""
        self._ignoring_aux_stream = False

    def stop(self):
        """Signals cancellation and immediately terminates yt-dlp + any child ffmpeg processes."""
        self._is_running = False
        terminate_process_tree(self._process)

    def run(self):
        ffmpeg_bin = get_ffmpeg_executable()
        ffmpeg_ok = bool(ffmpeg_bin)

        cmd = [
            get_ytdlp_executable(),
            "--newline",
            "--no-warnings",
            "-P", self.output_path,
        ]
        if ffmpeg_bin:
            cmd.extend(["--ffmpeg-location", os.path.dirname(ffmpeg_bin)])
        elif FFMPEG_LOCATION:
            cmd.extend(["--ffmpeg-location", FFMPEG_LOCATION])

        if self.concurrent_fragments > 1:
            cmd.extend(["--concurrent-fragments", str(self.concurrent_fragments)])

        if self.cookie_args:
            cmd.extend(self.cookie_args)

        # Temp tracker file to capture exact post-processed output path(s)
        tracker_file = None
        try:
            fd, tracker_file = tempfile.mkstemp(prefix="omnistream_path_", suffix=".txt")
            os.close(fd)
            escaped_tracker = tracker_file.replace("%", "%%")
            cmd.extend(["--print-to-file", "after_move:filepath", escaped_tracker])
        except Exception:
            tracker_file = None

        error_occurred = False
        error_message = ""

        try:
            is_audio_only = self.filetype.startswith("Audio Only")
            target_ext = None

            # --- 1. Format & Container Selection ---
            if is_audio_only:
                if "MP3" in self.filetype:
                    target_ext = "mp3"
                elif "M4A" in self.filetype:
                    target_ext = "m4a"
                elif "OPUS" in self.filetype:
                    target_ext = "opus"
                elif "FLAC" in self.filetype:
                    target_ext = "flac"
                elif "WAV" in self.filetype:
                    target_ext = "wav"

                if ffmpeg_ok:
                    cmd.extend([
                        "-f", "bestaudio[acodec!*=iamf][acodec!=ec-3][acodec!=ac-3]/bestaudio/best",
                        "-x"
                    ])
                    if target_ext in ("mp3", "m4a", "opus"):
                        cmd.extend(["--audio-format", target_ext, "--audio-quality", "0"])
                    elif target_ext in ("wav", "flac"):
                        cmd.extend(["--audio-format", target_ext])
                else:
                    self.output_signal.output_received.emit(
                        "Notice: FFmpeg is not installed — downloading best native audio stream without conversion.\n"
                    )
                    cmd.extend(["-f", "bestaudio[ext=m4a]/bestaudio[acodec!*=iamf]/bestaudio/best"])
            else:
                if "MP4" in self.filetype:
                    target_ext = "mp4"
                elif "MKV" in self.filetype:
                    target_ext = "mkv"
                elif "WebM" in self.filetype:
                    target_ext = "webm"

                height = None
                if self.quality.endswith("p"):
                    try:
                        height = int(self.quality[:-1])
                    except ValueError:
                        self.output_signal.output_received.emit(
                            f"Warning: Invalid quality '{self.quality}'. Falling back to Best Available.\n"
                        )

                if ffmpeg_ok:
                    if target_ext == "webm":
                        if height:
                            fmt = (
                                f"bestvideo[height<={height}][ext=webm]+bestaudio[ext=webm]/"
                                f"best[height<={height}][ext=webm]/"
                                f"bestvideo[height<={height}]+bestaudio[acodec!*=iamf]/best[height<={height}]"
                            )
                        else:
                            fmt = (
                                "bestvideo[ext=webm]+bestaudio[ext=webm]/best[ext=webm]/"
                                "bestvideo+bestaudio[acodec!*=iamf]/best"
                            )
                    else:
                        if height:
                            fmt = (
                                f"bestvideo[height<={height}]+bestaudio[acodec!*=iamf]/"
                                f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
                            )
                        else:
                            fmt = "bestvideo+bestaudio[acodec!*=iamf]/bestvideo+bestaudio/best"

                    cmd.extend(["-f", fmt])
                    if target_ext == "mp4":
                        # Preserve highest resolution first while preferring universal H.264 + AAC over AV1/EC-3
                        cmd.extend(["-S", "res,vcodec:h264:vp9:av01,acodec:aac:mp4a:opus"])
                    if target_ext:
                        cmd.extend(["--merge-output-format", target_ext])
                else:
                    self.output_signal.output_received.emit(
                        "Notice: FFmpeg not found — falling back to pre-merged single stream. "
                        "Install FFmpeg ('winget install Gyan.FFmpeg') for 1080p+/4K merging.\n"
                    )
                    fmt = f"best[height<={height}]/best" if height else "best"
                    cmd.extend(["-f", fmt])

            # --- 2. Lecture & Media Post-Processing Options ---
            if ffmpeg_ok:
                if self.embed_metadata:
                    cmd.append("--embed-metadata")
                    if not self.embed_chapters:
                        cmd.append("--no-embed-chapters")
                    # Embed cover art for containers that support attached pictures cleanly
                    if target_ext not in ("wav", "webm"):
                        cmd.append("--embed-thumbnail")
                elif self.embed_chapters:
                    cmd.append("--embed-chapters")

                if self.sponsorblock:
                    cmd.extend(["--sponsorblock-remove", "sponsor"])

                if self.clip_section and not self.is_playlist:
                    cmd.extend(["--download-sections", f"*{self.clip_section}"])

            if self.download_subs:
                cmd.extend(["--write-subs", "--write-auto-subs", "--sub-langs", self.sub_langs])
                if not self.is_playlist:
                    # Prevent an unavailable/rate-limited secondary caption from aborting the main video download
                    cmd.append("--ignore-errors")
                if ffmpeg_ok:
                    if is_audio_only:
                        # Save readable .srt lecture transcript alongside the audio file
                        cmd.extend(["--convert-subs", "srt"])
                    elif target_ext == "webm":
                        # WebM embeds WebVTT natively
                        cmd.append("--embed-subs")
                    else:
                        # Embed captions into MP4/MKV and convert standalone subs to .srt
                        cmd.extend(["--embed-subs", "--convert-subs", "srt"])

            # --- 3. Playlist vs Single Output Templates ---
            if self.is_playlist:
                folder_tpl = self.custom_name if self.custom_name else "%(playlist_title)s"
                cmd.extend([
                    "--yes-playlist",
                    "--ignore-errors",
                    "--no-abort-on-error",
                    "-o", f"{folder_tpl}/%(playlist_index)02d - %(title)s.%(ext)s"
                ])
                if self.playlist_end:
                    cmd.extend(["--playlist-end", str(self.playlist_end)])
            else:
                if self.playlist_items:
                    # Pure playlist URL where user requested only the first video
                    cmd.extend(["--playlist-items", self.playlist_items])
                else:
                    cmd.append("--no-playlist")

                if self.custom_name:
                    cmd.extend(["-o", f"{self.custom_name}.%(ext)s"])
                else:
                    cmd.extend(["-o", "%(title)s.%(ext)s"])

            cmd.append(self.url)
            self.output_signal.output_received.emit(f"Executing: {' '.join(cmd)}\n")

            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding='utf-8', errors='replace', bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )

            if self._process.stdout is not None:
                with self._process.stdout:
                    for line in iter(self._process.stdout.readline, ''):
                        if not self._is_running:
                            terminate_process_tree(self._process)
                            self.output_signal.output_received.emit("Termination signal sent.\n")
                            break
                        self.output_signal.output_received.emit(line)
                        self._parse_progress(line)
            self._process.wait()

            # Read exact output filepath from tracker file if produced
            if tracker_file and os.path.isfile(tracker_file):
                try:
                    with open(tracker_file, "r", encoding="utf-8", errors="replace") as tf:
                        paths = [ln.strip() for ln in tf.read().splitlines() if ln.strip()]
                    if paths:
                        self._detected_filepath = paths[-1]
                except Exception:
                    pass

            if self._detected_filepath:
                self.output_signal.file_produced.emit(self._detected_filepath)

            if not self._is_running:
                error_occurred = True
                error_message = "Download cancelled by user."
            elif self.is_playlist and self._process.returncode in (1, 101):
                if self.playlist_finished or self.seen_items > 0 or self.downloaded_count > 0:
                    self.output_signal.output_received.emit(
                        "\n✅ Playlist download finished! (Unavailable/blocked tracks were skipped).\n"
                    )
                    self.output_signal.download_finished.emit(
                        True, "Playlist downloaded (skipped unavailable tracks)."
                    )
                    return
                else:
                    error_occurred = True
                    error_message = f"Download failed (exit code: {self._process.returncode}). Check activity log."
            elif self._process.returncode != 0:
                if (
                    not self.is_playlist
                    and self._detected_filepath
                    and os.path.isfile(self._detected_filepath)
                    and os.path.getsize(self._detected_filepath) > 0
                ):
                    self.output_signal.output_received.emit(
                        "\n✅ Media file downloaded! (An optional subtitle/metadata track was skipped).\n"
                    )
                    self.output_signal.download_finished.emit(
                        True, "Downloaded (some optional subtitles were skipped)."
                    )
                    return
                error_occurred = True
                error_message = f"Download failed (exit code: {self._process.returncode}). Check activity log."
            else:
                self.output_signal.output_received.emit("Download completed successfully.\n")
        except FileNotFoundError:
            error_message = "Error: yt-dlp command not found in system PATH."
            error_occurred = True
        except Exception as e:
            error_message = f"Error during download: {e}"
            error_occurred = True
        finally:
            self._process = None
            if tracker_file and os.path.exists(tracker_file):
                try:
                    os.remove(tracker_file)
                except Exception:
                    pass

        self.output_signal.download_finished.emit(
            not error_occurred,
            error_message if error_occurred else "Download finished."
        )

    def _parse_progress(self, line: str):
        match_item = re.search(r"\[download\]\s+Downloading\s+item\s+(\d+)\s+of\s+(\d+)", line)
        if match_item:
            cur = int(match_item.group(1))
            tot = int(match_item.group(2))
            self.seen_items = cur
            self._ignoring_aux_stream = False
            self.output_signal.stats_updated.emit("", f"Item {cur}/{tot}", "")
            self.output_signal.output_received.emit(f"\n>>> Downloading Item {cur} of {tot} ...\n")

        # Track whether the active [download] block is an auxiliary subtitle/thumbnail file vs main media
        m_dest = re.search(r"\[download\] Destination:\s+(.+)", line)
        if m_dest:
            dest_candidate = m_dest.group(1).strip().strip('"')
            lower_dest = dest_candidate.lower()
            self._ignoring_aux_stream = lower_dest.endswith(AUXILIARY_DOWNLOAD_EXTS)
            if dest_candidate and not self._ignoring_aux_stream:
                self._detected_filepath = dest_candidate

        if "Finished downloading playlist" in line:
            self.playlist_finished = True
        elif not self._ignoring_aux_stream and ("[download] 100%" in line or "100% of" in line):
            self.downloaded_count += 1

        if not self._ignoring_aux_stream:
            # Fixed decimal regex so fractional progress (e.g. 45.3%) updates smoothly in real time
            match_pct = re.search(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%", line)
            if match_pct:
                try:
                    percent = min(100, max(0, int(round(float(match_pct.group(1))))))
                    self.output_signal.progress_updated.emit(percent)
                except ValueError:
                    pass

            match_speed = re.search(r"at\s+([\d\.]+\s*[A-Za-z]+/s)", line)
            match_eta = re.search(r"ETA\s+([\d:]+)", line)
            match_size = re.search(r"of\s+~?\s*([\d\.]+\s*[A-Za-z]+)", line)
            speed = match_speed.group(1) if match_speed else ""
            eta = match_eta.group(1) if match_eta else ""
            size = match_size.group(1) if match_size else ""
            if speed or eta or size:
                self.output_signal.stats_updated.emit(speed, eta, size)
                return

        # Fallback output path capture from post-processing / already-downloaded lines
        for pattern in (
            r'\[Merger\] Merging formats into "(.+?)"',
            r"\[ExtractAudio\] Destination:\s+(.+)",
            r"\[download\]\s+(.+?)\s+has already been downloaded",
        ):
            m_path = re.search(pattern, line)
            if m_path:
                candidate = m_path.group(1).strip().strip('"')
                if candidate and not candidate.lower().endswith(AUXILIARY_DOWNLOAD_EXTS):
                    self._detected_filepath = candidate
                break

        # Post-processing stage indicators for live HUD feedback
        if "[Merger]" in line:
            self.output_signal.stage_updated.emit("🎬 Merging video & audio streams...")
        elif "[ExtractAudio]" in line:
            self.output_signal.stage_updated.emit("🎵 Extracting & converting audio...")
        elif "[EmbedSubtitle]" in line:
            self.output_signal.stage_updated.emit("💬 Embedding subtitles...")
        elif "[EmbedThumbnail]" in line:
            self.output_signal.stage_updated.emit("🖼 Embedding cover art...")
        elif "[Metadata]" in line:
            self.output_signal.stage_updated.emit("🏷 Writing metadata & chapters...")
        elif "[ModifyChapters]" in line or "[SponsorBlock]" in line:
            self.output_signal.stage_updated.emit("✂ Applying chapters / SponsorBlock...")


class MetadataFetcherThread(QThread):
    """Fetches video metadata using yt-dlp -J in a cancellable background thread."""
    def __init__(
        self,
        request_id: int,
        url: str,
        metadata_signal: MetadataSignal,
        cookie_args: Optional[List[str]] = None
    ):
        super().__init__()
        self.request_id = request_id
        self.url = url
        self.metadata_signal = metadata_signal
        self.cookie_args = cookie_args or []
        self._aborted = False
        self._process = None

    def abort(self):
        self._aborted = True
        terminate_process_tree(self._process)

    def run(self):
        try:
            ytdlp_bin = get_ytdlp_executable()
            # Fast, non-blocking metadata extraction with playlist and mix support
            cmd = [
                ytdlp_bin,
                "--no-warnings",
                "--skip-download",
                "-J",
                "--flat-playlist",
                "--ignore-errors"
            ]
            ffmpeg_bin = get_ffmpeg_executable()
            if ffmpeg_bin:
                cmd.extend(["--ffmpeg-location", os.path.dirname(ffmpeg_bin)])
            elif FFMPEG_LOCATION:
                cmd.extend(["--ffmpeg-location", FFMPEG_LOCATION])
            if self.cookie_args:
                cmd.extend(self.cookie_args)
            cmd.append(self.url)

            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding='utf-8', errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            stdout_raw, stderr_raw = self._process.communicate()
            returncode = self._process.returncode
            self._process = None

            if self._aborted:
                return

            stdout = (stdout_raw or "").strip()
            stderr = (stderr_raw or "").strip()

            video_info = None
            if stdout and stdout != "null":
                try:
                    parsed = json.loads(stdout)
                    if isinstance(parsed, dict) and parsed:
                        video_info = parsed
                except Exception:
                    video_info = None

            # If no valid dictionary metadata was parsed (or yt-dlp errored out with empty/null output)
            if video_info is None or not isinstance(video_info, dict):
                err_text = ""
                if stderr:
                    error_lines = []
                    for line in stderr.splitlines():
                        line_s = line.strip()
                        if "ERROR:" in line_s:
                            clean_line = line_s.split("ERROR:", 1)[-1].strip()
                            if clean_line:
                                error_lines.append(clean_line)
                    if error_lines:
                        err_text = " | ".join(error_lines[:2])
                    else:
                        err_text = stderr.splitlines()[-1].strip()

                if not err_text:
                    if returncode != 0:
                        err_text = f"yt-dlp exited with error code {returncode}"
                    else:
                        err_text = "No stream metadata found for this URL"

                if not self._aborted:
                    self.metadata_signal.error_occurred.emit(self.request_id, err_text)
                return

            if not self._aborted:
                self.metadata_signal.metadata_ready.emit(self.request_id, video_info)
        except FileNotFoundError:
            if not self._aborted:
                self.metadata_signal.error_occurred.emit(self.request_id, "yt-dlp executable not found in PATH.")
        except Exception as e:
            if not self._aborted:
                self.metadata_signal.error_occurred.emit(self.request_id, f"Metadata fetch error: {e}")
        finally:
            self._process = None


class EngineUpdateThread(QThread):
    """Executes yt-dlp -U (with automatic pip fallback in virtual environments) to upgrade yt-dlp."""
    output_received = pyqtSignal(str)
    update_finished = pyqtSignal(bool, str)

    def run(self):
        try:
            self.output_received.emit("\n--- Checking for yt-dlp Engine Updates ---\n")
            collected = []
            process = subprocess.Popen(
                [get_ytdlp_executable(), "-U"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding='utf-8', errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            if process.stdout is not None:
                with process.stdout:
                    for line in iter(process.stdout.readline, ''):
                        collected.append(line)
                        self.output_received.emit(line)
            process.wait()

            combined_out = "".join(collected).lower()
            if process.returncode != 0 and "pip" in combined_out and not getattr(sys, "frozen", False):
                self.output_received.emit("Detected pip-managed yt-dlp — upgrading via pip...\n")
                pip_proc = subprocess.Popen(
                    [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding='utf-8', errors='replace',
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
                if pip_proc.stdout is not None:
                    with pip_proc.stdout:
                        for line in iter(pip_proc.stdout.readline, ''):
                            self.output_received.emit(line)
                pip_proc.wait()
                success = pip_proc.returncode == 0
            else:
                success = process.returncode == 0

            msg = "yt-dlp engine is up to date!" if success else f"Update exited with code {process.returncode}"
            self.update_finished.emit(success, msg)
        except Exception as e:
            self.output_received.emit(f"Update error: {e}\n")
            self.update_finished.emit(False, str(e))


def ensure_arrow_icons():
    """Generates crisp modern antialiased vector chevron icons in app/cache directory."""
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()

    if not os.access(base_dir, os.W_OK):
        base_dir = QStandardPaths.writableLocation(QStandardPaths.TempLocation) or tempfile.gettempdir()

    normal_icon = os.path.join(base_dir, "chevron_down.png").replace("\\", "/")
    hover_icon = os.path.join(base_dir, "chevron_down_hover.png").replace("\\", "/")

    if not (os.path.exists(normal_icon) and os.path.exists(hover_icon)):
        try:
            for icon_path, color_str, width in [(normal_icon, "#94A3B8", 2.2), (hover_icon, "#00DFD8", 2.5)]:
                pix = QPixmap(24, 24)
                pix.fill(Qt.transparent)
                painter = QPainter(pix)
                painter.setRenderHint(QPainter.Antialiasing)
                pen = QPen(QColor(color_str), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                painter.setPen(pen)
                painter.drawLine(QPointF(6, 9), QPointF(12, 15))
                painter.drawLine(QPointF(12, 15), QPointF(18, 9))
                painter.end()
                pix.save(icon_path, "PNG")
        except Exception:
            pass

    return normal_icon, hover_icon


# --- Main Window ---
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self.resize(DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
        self.center_window()
        self.setAcceptDrops(True)

        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)

        # Sensible default download directory: user Downloads folder (check saved preference first)
        candidates = [
            self.settings.value("output_dir", "", type=str),
            os.path.join(os.path.expanduser("~"), "Downloads"),
            os.path.join(os.path.expanduser("~"), "Desktop"),
            r"D:\angel\Downloads",
            r"D:\angel\Desktop"
        ]
        self.output_dir = next((p for p in candidates if p and os.path.isdir(p)), os.path.expanduser("~"))

        self.video_info = {}
        self.download_thread = None
        self.metadata_thread = None
        self.thumbnail_thread = None
        self._retired_threads = []
        self._active_request_id = 0
        self.quality_data = {}
        self.is_log_expanded = self.settings.value("is_log_expanded", True, type=bool)
        self.last_downloaded_file = ""
        self._preferred_filetype = self.settings.value("preferred_filetype", "MP4 (Universal Video)", type=str)

        # Signals
        self.output_signal = OutputSignal()
        self.output_signal.output_received.connect(self.update_log)
        self.output_signal.progress_updated.connect(self.update_progress)
        self.output_signal.stats_updated.connect(self.update_stats)
        self.output_signal.stage_updated.connect(self.update_stage)
        self.output_signal.file_produced.connect(self.on_file_produced)
        self.output_signal.download_finished.connect(self.on_download_complete)

        self.metadata_signal = MetadataSignal()
        self.metadata_signal.metadata_ready.connect(self.on_metadata_ready)
        self.metadata_signal.error_occurred.connect(self.on_metadata_error)

        self.thumbnail_signal = ThumbnailSignal()
        self.thumbnail_signal.thumbnail_ready.connect(self.on_thumbnail_ready)
        self.thumbnail_signal.error_occurred.connect(self.on_thumbnail_error)

        # UI & Styling
        self.init_ui()
        self.apply_styles()
        self.restore_preferences()
        self.check_ytdlp_exists()

    def center_window(self):
        """Centers the window on the active desktop screen."""
        qr = self.frameGeometry()
        cp = QDesktopWidget().availableGeometry().center()
        qr.moveCenter(cp)
        self.move(qr.topLeft())

    def _retire_thread(self, thread):
        """Safely keeps a reference to an aborted QThread until it exits run() to prevent GC crashes."""
        if thread is None:
            return
        if thread.isRunning():
            self._retired_threads.append(thread)
            thread.finished.connect(lambda t=thread: self._cleanup_retired_thread(t))

    def _cleanup_retired_thread(self, thread):
        if thread in self._retired_threads:
            self._retired_threads.remove(thread)

    # --- Drag & Drop URL Support ---
    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if self.download_thread and self.download_thread.isRunning():
            return
        mime = event.mimeData()
        dropped_url = ""
        if mime.hasUrls():
            for qurl in mime.urls():
                s = qurl.toString().strip()
                if s.startswith("http://") or s.startswith("https://"):
                    dropped_url = s
                    break
        if not dropped_url and mime.hasText():
            txt = mime.text().strip()
            match = re.search(r"(https?://\S+)", txt)
            dropped_url = match.group(1) if match else txt

        if dropped_url:
            self.metadata_fetch_timer.stop()
            self.url_input.blockSignals(True)
            self.url_input.setText(dropped_url)
            self.url_input.blockSignals(False)
            self.fetch_metadata()
            event.acceptProposedAction()

    # --- Engine Verification (yt-dlp + FFmpeg) ---
    def check_ytdlp_exists(self):
        """Checks if yt-dlp and FFmpeg are accessible; displays setup guidance if missing."""
        ytdlp_bin = get_ytdlp_executable()
        ytdlp_ok = False
        try:
            res = subprocess.run(
                [ytdlp_bin, "--version"], capture_output=True, text=True, timeout=6,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            ytdlp_ok = (res.returncode == 0)
        except Exception:
            ytdlp_ok = False

        ffmpeg_ok = has_ffmpeg()

        if not ytdlp_ok:
            self.set_status_pill("● Setup Required", "#F59E0B", "#2A2318")
            self.status_label.setText("Engine Setup Required")
            self.update_log(
                "===========================================================\n"
                "  Notice: 'yt-dlp' engine is not yet installed on this PC.\n"
                "  Quick Setup (10 Seconds):\n"
                "  1. Open Command Prompt / PowerShell and run:\n"
                "         winget install yt-dlp\n"
                "         winget install Gyan.FFmpeg\n"
                "  2. Or download yt-dlp.exe & ffmpeg.exe from GitHub and\n"
                "     drop them into this application folder!\n"
                "===========================================================\n"
            )
            QTimer.singleShot(400, lambda: self.show_engine_setup_dialog(missing_ytdlp=True, missing_ffmpeg=not ffmpeg_ok))
        elif not ffmpeg_ok:
            self.set_status_pill("● Ready (No FFmpeg)", "#F59E0B", "#2A2318")
            self.status_label.setText("FFmpeg Recommended")
            self.update_log(
                "Notice: yt-dlp is ready, but FFmpeg was not detected in PATH.\n"
                "  • Basic streams will still download, but 1080p+/4K stream merging,\n"
                "    MP3 conversion, embedded subtitles/thumbnails, and clip trimming require FFmpeg.\n"
                "  • Install in 10 seconds via PowerShell:  winget install Gyan.FFmpeg\n"
            )
        else:
            self.set_status_pill("● Ready", "#10B981", "#1A2234")
            self.status_label.setText("Ready")

    def show_engine_setup_dialog(self, missing_ytdlp=True, missing_ffmpeg=False):
        """Shows a helpful setup dialog with WinGet copy and GitHub download buttons."""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("OmniStream Studio - Engine Setup")
        msg_box.setIcon(QMessageBox.Information)

        if missing_ytdlp:
            cmd_to_copy = "winget install yt-dlp && winget install Gyan.FFmpeg" if missing_ffmpeg else "winget install yt-dlp"
            msg_box.setText("<h3>Core Engine (yt-dlp) Required</h3>")
            msg_box.setInformativeText(
                "OmniStream Studio uses open-source <b>yt-dlp</b> (and <b>FFmpeg</b> for HD stream merging & MP3 conversion).<br><br>"
                "<b>Option 1 (Fastest & Automatic - 10 Seconds):</b><br>"
                "Open Command Prompt or PowerShell and run:<br>"
                f"<span style='color: #00DFD8; font-weight: bold;'>{cmd_to_copy}</span><br><br>"
                "<b>Option 2 (Manual from GitHub):</b><br>"
                "Download <b>yt-dlp.exe</b> (and <b>ffmpeg.exe</b>) from GitHub and place them in this folder."
            )
        else:
            cmd_to_copy = "winget install Gyan.FFmpeg"
            msg_box.setText("<h3>FFmpeg Recommended for Full Studio Features</h3>")
            msg_box.setInformativeText(
                "<b>FFmpeg</b> enables 1080p/4K video+audio merging, MP3/FLAC conversion, subtitle embedding, and clip trimming.<br><br>"
                "Open Command Prompt or PowerShell and run:<br>"
                f"<span style='color: #00DFD8; font-weight: bold;'>{cmd_to_copy}</span>"
            )

        copy_btn = msg_box.addButton("📋 Copy WinGet Command", QMessageBox.ActionRole)
        github_btn = msg_box.addButton("🌐 Open GitHub Release", QMessageBox.ActionRole)
        msg_box.addButton("OK", QMessageBox.RejectRole)

        msg_box.exec_()
        if msg_box.clickedButton() == copy_btn:
            QApplication.clipboard().setText(cmd_to_copy)
            self.update_log(f"Copied to clipboard: '{cmd_to_copy}'. Paste this into Terminal or PowerShell!\n")
        elif msg_box.clickedButton() == github_btn:
            url = "https://github.com/yt-dlp/yt-dlp/releases/latest" if missing_ytdlp else "https://github.com/yt-dlp/FFmpeg-Builds/releases"
            QDesktopServices.openUrl(QUrl(url))

    def init_ui(self):
        """Sets up the UI with a modern QScrollArea for easy vertical resizing on any monitor."""
        self.central_widget = QWidget()
        self.central_widget.setObjectName("centralWidget")
        self.setCentralWidget(self.central_widget)

        root_layout = QVBoxLayout(self.central_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("mainScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.content_widget = QWidget()
        self.content_widget.setObjectName("scrollContent")
        content_layout = QVBoxLayout(self.content_widget)
        content_layout.setSpacing(11)
        content_layout.setContentsMargins(22, 16, 22, 18)

        # ----------------- 1. APP HEADER -----------------
        header_layout = QHBoxLayout()
        header_layout.setSpacing(10)

        logo_badge = QLabel("⚡")
        logo_badge.setObjectName("logoBadge")
        logo_badge.setFixedSize(36, 36)
        logo_badge.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(logo_badge)

        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        app_title = QLabel("OmniStream Studio")
        app_title.setObjectName("appTitleLabel")
        app_subtitle = QLabel("Created by Angelo Golloberda • Universal Media & Stream Extractor")
        app_subtitle.setObjectName("appSubtitleLabel")
        title_box.addWidget(app_title)
        title_box.addWidget(app_subtitle)
        header_layout.addLayout(title_box, 1)

        self.status_pill = QLabel("● Ready")
        self.status_pill.setObjectName("statusPill")
        self.status_pill.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(self.status_pill)
        content_layout.addLayout(header_layout)

        # ----------------- 2. URL INPUT CARD -----------------
        url_card = QFrame()
        url_card.setObjectName("surfaceCard")
        url_card_layout = QVBoxLayout(url_card)
        url_card_layout.setContentsMargins(12, 10, 12, 10)
        url_card_layout.setSpacing(6)

        url_header = QLabel("MEDIA / STREAM URL  (PASTE OR DRAG & DROP LINK)")
        url_header.setObjectName("sectionHeaderLabel")
        url_card_layout.addWidget(url_header)

        url_input_layout = QHBoxLayout()
        url_input_layout.setSpacing(8)
        self.url_input = QLineEdit()
        self.url_input.setObjectName("urlInput")
        self.url_input.setPlaceholderText("Paste or drop link from Webex, YouTube, Panopto, Vimeo, Twitch, etc...")
        self.url_input.setMinimumHeight(36)

        self.metadata_fetch_timer = QTimer(self)
        self.metadata_fetch_timer.setSingleShot(True)
        self.metadata_fetch_timer.timeout.connect(self.fetch_metadata)
        self.url_input.textChanged.connect(self.schedule_metadata_fetch)
        self.url_input.returnPressed.connect(self.fetch_metadata)
        url_input_layout.addWidget(self.url_input, 1)

        self.paste_button = QPushButton("📋 Paste")
        self.paste_button.setObjectName("actionButton")
        self.paste_button.setCursor(QCursor(Qt.PointingHandCursor))
        self.paste_button.setToolTip("Paste from clipboard & fetch info")
        self.paste_button.clicked.connect(self.paste_and_fetch)
        url_input_layout.addWidget(self.paste_button)

        self.clear_button = QPushButton("✕ Clear")
        self.clear_button.setObjectName("ghostButton")
        self.clear_button.setCursor(QCursor(Qt.PointingHandCursor))
        self.clear_button.setToolTip("Clear link and reset form")
        self.clear_button.clicked.connect(self.clear_url_input)
        url_input_layout.addWidget(self.clear_button)

        url_card_layout.addLayout(url_input_layout)
        content_layout.addWidget(url_card)

        # ----------------- 3. AUTHENTICATION & COOKIES CARD -----------------
        self.cookie_card = QFrame()
        self.cookie_card.setObjectName("surfaceCard")
        cookie_layout = QVBoxLayout(self.cookie_card)
        cookie_layout.setContentsMargins(12, 9, 12, 9)
        cookie_layout.setSpacing(6)

        cookie_header_layout = QHBoxLayout()
        cookie_title = QLabel("🍪 AUTHENTICATION & COOKIES (FOR WEBEX & UNIVERSITY LOGINS)")
        cookie_title.setObjectName("sectionHeaderLabel")
        cookie_header_layout.addWidget(cookie_title)
        cookie_header_layout.addStretch()
        cookie_layout.addLayout(cookie_header_layout)

        cookie_inputs_layout = QHBoxLayout()
        cookie_inputs_layout.setSpacing(8)

        self.cookie_source_combo = QComboBox()
        self.cookie_source_combo.setObjectName("customComboBox")
        self.cookie_source_combo.setMinimumHeight(34)
        self.cookie_source_combo.addItem("None (Public Videos)")
        self.cookie_source_combo.addItem("Browser: Google Chrome")
        self.cookie_source_combo.addItem("Browser: Microsoft Edge")
        self.cookie_source_combo.addItem("Browser: Mozilla Firefox")
        self.cookie_source_combo.addItem("Browser: Brave")
        self.cookie_source_combo.addItem("Custom File (cookies.txt)")
        self.cookie_source_combo.currentIndexChanged.connect(self.on_cookie_source_changed)
        cookie_inputs_layout.addWidget(self.cookie_source_combo, 1)

        self.cookie_file_input = QLineEdit()
        self.cookie_file_input.setObjectName("outputDirField")
        self.cookie_file_input.setPlaceholderText("Select cookies.txt file...")
        self.cookie_file_input.setReadOnly(True)
        self.cookie_file_input.setEnabled(False)
        cookie_inputs_layout.addWidget(self.cookie_file_input, 2)

        self.cookie_browse_button = QPushButton("📂 Browse...")
        self.cookie_browse_button.setObjectName("ghostButton")
        self.cookie_browse_button.setCursor(QCursor(Qt.PointingHandCursor))
        self.cookie_browse_button.setEnabled(False)
        self.cookie_browse_button.clicked.connect(self.choose_cookie_file)
        cookie_inputs_layout.addWidget(self.cookie_browse_button)

        cookie_layout.addLayout(cookie_inputs_layout)

        self.cookie_hint_label = QLabel(
            "💡 Log in to Webex or your university portal in your browser, then select that browser above to download."
        )
        self.cookie_hint_label.setObjectName("hintLabel")
        self.cookie_hint_label.setWordWrap(True)
        cookie_layout.addWidget(self.cookie_hint_label)

        content_layout.addWidget(self.cookie_card)

        # ----------------- 4. RICH MEDIA PREVIEW CARD -----------------
        self.media_card = QFrame()
        self.media_card.setObjectName("surfaceCard")
        media_card_layout = QHBoxLayout(self.media_card)
        media_card_layout.setContentsMargins(12, 10, 12, 10)
        media_card_layout.setSpacing(14)

        self.thumbnail_label = QLabel("▶")
        self.thumbnail_label.setObjectName("thumbnailContainer")
        self.thumbnail_label.setFixedSize(200, 112)
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        media_card_layout.addWidget(self.thumbnail_label)

        info_block = QVBoxLayout()
        info_block.setSpacing(4)
        info_block.setAlignment(Qt.AlignVCenter)

        self.video_title_label = QLabel("Paste a video link above")
        self.video_title_label.setObjectName("videoTitleLabel")
        self.video_title_label.setWordWrap(True)
        info_block.addWidget(self.video_title_label)

        self.channel_badge = QLabel("Waiting for URL input...")
        self.channel_badge.setObjectName("channelBadge")
        info_block.addWidget(self.channel_badge)

        badges_row = QHBoxLayout()
        badges_row.setSpacing(6)

        self.duration_badge = QLabel("--:--")
        self.duration_badge.setObjectName("metaBadge")
        badges_row.addWidget(self.duration_badge)

        self.views_badge = QLabel("0 views")
        self.views_badge.setObjectName("metaBadge")
        badges_row.addWidget(self.views_badge)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("metaBadgeHighlight")
        badges_row.addWidget(self.status_label)
        badges_row.addStretch()

        info_block.addLayout(badges_row)
        media_card_layout.addLayout(info_block, 1)
        content_layout.addWidget(self.media_card)

        # ----------------- 5. FORMAT, SPEED & LECTURE ENHANCEMENTS CARD -----------------
        settings_card = QFrame()
        settings_card.setObjectName("surfaceCard")
        settings_outer = QVBoxLayout(settings_card)
        settings_outer.setContentsMargins(12, 9, 12, 10)
        settings_outer.setSpacing(8)

        settings_layout = QGridLayout()
        settings_layout.setHorizontalSpacing(12)
        settings_layout.setVerticalSpacing(5)

        quality_header = QLabel("RESOLUTION / VIDEO QUALITY")
        quality_header.setObjectName("sectionHeaderLabel")
        format_header = QLabel("MEDIA CONTAINER / AUDIO FORMAT")
        format_header.setObjectName("sectionHeaderLabel")
        speed_header = QLabel("STREAM ACCELERATION")
        speed_header.setObjectName("sectionHeaderLabel")

        settings_layout.addWidget(quality_header, 0, 0)
        settings_layout.addWidget(format_header, 0, 1)
        settings_layout.addWidget(speed_header, 0, 2)
        settings_layout.setColumnStretch(0, 3)
        settings_layout.setColumnStretch(1, 3)
        settings_layout.setColumnStretch(2, 2)

        self.quality_combo = QComboBox()
        self.quality_combo.setObjectName("customComboBox")
        self.quality_combo.setMinimumHeight(34)
        self.quality_combo.setEnabled(False)
        self.quality_combo.addItem("Waiting for video...")
        settings_layout.addWidget(self.quality_combo, 1, 0)

        self.filetype_combo = QComboBox()
        self.filetype_combo.setObjectName("customComboBox")
        self.filetype_combo.setMinimumHeight(34)
        self.filetype_combo.setEnabled(False)
        self.filetype_combo.addItem("Waiting for video...")
        self.filetype_combo.currentTextChanged.connect(self.on_filetype_changed)
        settings_layout.addWidget(self.filetype_combo, 1, 1)

        self.speed_combo = QComboBox()
        self.speed_combo.setObjectName("customComboBox")
        self.speed_combo.setMinimumHeight(34)
        self.speed_combo.setToolTip(
            "Multi-threaded HLS/DASH fragment downloading.\n"
            "4x–8x dramatically speeds up Webex, Panopto, Vimeo, and Twitch streams."
        )
        self.speed_combo.addItem("⚡ 4x Fast", 4)
        self.speed_combo.addItem("🚀 8x Turbo", 8)
        self.speed_combo.addItem("🔥 16x Ultra", 16)
        self.speed_combo.addItem("1x Standard", 1)
        self.speed_combo.currentIndexChanged.connect(self.save_preferences)
        settings_layout.addWidget(self.speed_combo, 1, 2)

        # Playlist Scope Selector (Shown when a playlist / mix is detected)
        self.playlist_scope_label = QLabel("📚 Download Scope:")
        self.playlist_scope_label.setObjectName("boldLabel")
        self.playlist_scope_label.setVisible(False)
        settings_layout.addWidget(self.playlist_scope_label, 2, 0)

        self.playlist_scope_combo = QComboBox()
        self.playlist_scope_combo.setObjectName("customComboBox")
        self.playlist_scope_combo.setMinimumHeight(34)
        self.playlist_scope_combo.setVisible(False)
        self.playlist_scope_combo.currentIndexChanged.connect(self.on_playlist_scope_changed)
        settings_layout.addWidget(self.playlist_scope_combo, 2, 1, 1, 2)

        settings_outer.addLayout(settings_layout)

        # Lecture & Study Enhancements Pill Bar
        enhancements_header = QLabel("LECTURE & STUDIO ENHANCEMENTS")
        enhancements_header.setObjectName("sectionHeaderLabel")
        settings_outer.addWidget(enhancements_header)

        pills_row = QHBoxLayout()
        pills_row.setSpacing(7)

        self.embed_meta_btn = self._create_toggle_pill(
            "🖼 Cover Art & Tags",
            "Embed title, speaker/artist, upload date, and thumbnail cover art into downloaded media",
            checked=True
        )
        pills_row.addWidget(self.embed_meta_btn)

        self.subs_btn = self._create_toggle_pill(
            "💬 Subtitles / Captions",
            "Download & embed subtitles/auto-captions (saves .srt transcript alongside audio lectures)",
            checked=False
        )
        pills_row.addWidget(self.subs_btn)

        self.chapters_btn = self._create_toggle_pill(
            "📑 Chapters",
            "Preserve lecture agenda & timestamp chapter markers inside the media container",
            checked=True
        )
        pills_row.addWidget(self.chapters_btn)

        self.sponsorblock_btn = self._create_toggle_pill(
            "⏭ SponsorBlock",
            "Automatically remove sponsored segments using the SponsorBlock database",
            checked=False
        )
        pills_row.addWidget(self.sponsorblock_btn)

        self.trim_btn = self._create_toggle_pill(
            "✂ Trim Clip",
            "Download only a specific timestamp range (e.g. 00:10:00 to 00:25:30) without downloading the full stream",
            checked=False,
            persist=False
        )
        self.trim_btn.toggled.connect(self.on_trim_toggled)
        pills_row.addWidget(self.trim_btn)
        pills_row.addStretch()

        settings_outer.addLayout(pills_row)

        # Collapsible Clip Timestamp Row
        self.trim_frame = QFrame()
        self.trim_frame.setObjectName("subPanel")
        self.trim_frame.setVisible(False)
        trim_layout = QHBoxLayout(self.trim_frame)
        trim_layout.setContentsMargins(10, 6, 10, 6)
        trim_layout.setSpacing(8)

        trim_lbl = QLabel("✂ Clip Range:")
        trim_lbl.setObjectName("boldLabel")
        trim_layout.addWidget(trim_lbl)

        self.trim_start_input = QLineEdit()
        self.trim_start_input.setObjectName("urlInput")
        self.trim_start_input.setPlaceholderText("Start (e.g. 00:02:30)")
        self.trim_start_input.setFixedWidth(145)
        trim_layout.addWidget(self.trim_start_input)

        arrow_lbl = QLabel("→")
        arrow_lbl.setObjectName("boldLabel")
        trim_layout.addWidget(arrow_lbl)

        self.trim_end_input = QLineEdit()
        self.trim_end_input.setObjectName("urlInput")
        self.trim_end_input.setPlaceholderText("End (blank = end)")
        self.trim_end_input.setFixedWidth(145)
        trim_layout.addWidget(self.trim_end_input)

        trim_hint = QLabel("Formats: MM:SS or HH:MM:SS")
        trim_hint.setObjectName("hintLabel")
        trim_layout.addWidget(trim_hint, 1)

        settings_outer.addWidget(self.trim_frame)
        content_layout.addWidget(settings_card)

        # ----------------- 6. DESTINATION FOLDER & CUSTOM FILENAME CARD -----------------
        folder_card = QFrame()
        folder_card.setObjectName("surfaceCard")
        folder_outer = QVBoxLayout(folder_card)
        folder_outer.setContentsMargins(12, 8, 12, 8)
        folder_outer.setSpacing(6)

        folder_card_layout = QHBoxLayout()
        folder_card_layout.setSpacing(8)

        folder_icon = QLabel("📁")
        folder_icon.setObjectName("iconLabel")
        folder_card_layout.addWidget(folder_icon)

        folder_label_title = QLabel("Save To:")
        folder_label_title.setObjectName("boldLabel")
        folder_label_title.setMinimumWidth(58)
        folder_card_layout.addWidget(folder_label_title)

        self.output_dir_label = QLineEdit(self.output_dir)
        self.output_dir_label.setObjectName("outputDirField")
        self.output_dir_label.setReadOnly(True)
        self.output_dir_label.setToolTip(self.output_dir)
        folder_card_layout.addWidget(self.output_dir_label, 1)

        self.choose_dir_button = QPushButton("📂 Browse...")
        self.choose_dir_button.setObjectName("ghostButton")
        self.choose_dir_button.setCursor(QCursor(Qt.PointingHandCursor))
        self.choose_dir_button.setToolTip("Select download directory")
        self.choose_dir_button.clicked.connect(self.choose_output_folder)
        folder_card_layout.addWidget(self.choose_dir_button)

        self.play_file_button = QPushButton("▶ Play")
        self.play_file_button.setObjectName("ghostButton")
        self.play_file_button.setCursor(QCursor(Qt.PointingHandCursor))
        self.play_file_button.setToolTip("Play last downloaded file")
        self.play_file_button.setEnabled(False)
        self.play_file_button.clicked.connect(self.play_last_file)
        folder_card_layout.addWidget(self.play_file_button)

        self.open_folder_button = QPushButton("Reveal")
        self.open_folder_button.setObjectName("ghostButton")
        self.open_folder_button.setCursor(QCursor(Qt.PointingHandCursor))
        self.open_folder_button.setToolTip("Reveal in Windows Explorer")
        self.open_folder_button.clicked.connect(self.open_output_folder)
        folder_card_layout.addWidget(self.open_folder_button)

        folder_outer.addLayout(folder_card_layout)

        # Inline Custom Filename / Folder Name Row
        rename_row = QHBoxLayout()
        rename_row.setSpacing(8)

        rename_icon = QLabel("✏️")
        rename_icon.setObjectName("iconLabel")
        rename_row.addWidget(rename_icon)

        rename_label = QLabel("Save As:")
        rename_label.setObjectName("boldLabel")
        rename_label.setMinimumWidth(58)
        rename_row.addWidget(rename_label)

        self.custom_filename_input = QLineEdit()
        self.custom_filename_input.setObjectName("urlInput")
        self.custom_filename_input.setPlaceholderText("Optional custom filename (leave blank to use original title)...")
        rename_row.addWidget(self.custom_filename_input, 1)

        folder_outer.addLayout(rename_row)
        content_layout.addWidget(folder_card)

        # ----------------- 7. DOWNLOAD ACTION & PROGRESS HUD -----------------
        action_card = QFrame()
        action_card.setObjectName("surfaceCard")
        action_layout = QVBoxLayout(action_card)
        action_layout.setContentsMargins(12, 10, 12, 10)
        action_layout.setSpacing(8)

        self.download_button = QPushButton("⬇  Start Download")
        self.download_button.setObjectName("downloadButton")
        self.download_button.setCursor(QCursor(Qt.PointingHandCursor))
        self.download_button.setMinimumHeight(42)
        self.download_button.clicked.connect(self.handle_download_button_click)
        self.download_button.setEnabled(False)
        action_layout.addWidget(self.download_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("downloadProgressBar")
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(10)
        action_layout.addWidget(self.progress_bar)

        hud_layout = QHBoxLayout()
        self.hud_speed_size = QLabel("⚡ Ready to download")
        self.hud_speed_size.setObjectName("hudTextLeft")
        hud_layout.addWidget(self.hud_speed_size)
        hud_layout.addStretch()

        self.hud_eta_percent = QLabel("0%")
        self.hud_eta_percent.setObjectName("hudTextRight")
        hud_layout.addWidget(self.hud_eta_percent)
        action_layout.addLayout(hud_layout)

        content_layout.addWidget(action_card)

        # ----------------- 8. CONSOLE LOG DRAWER -----------------
        self.log_card = QFrame()
        self.log_card.setObjectName("surfaceCard")
        log_layout = QVBoxLayout(self.log_card)
        log_layout.setContentsMargins(12, 8, 12, 8)
        log_layout.setSpacing(6)

        log_header_layout = QHBoxLayout()
        log_title = QLabel("💻 ACTIVITY CONSOLE")
        log_title.setObjectName("sectionHeaderLabel")
        log_header_layout.addWidget(log_title)
        log_header_layout.addStretch()

        self.toggle_log_btn = QPushButton("▾ Collapse" if self.is_log_expanded else "▸ Expand")
        self.toggle_log_btn.setObjectName("microButton")
        self.toggle_log_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.toggle_log_btn.clicked.connect(self.toggle_console)
        log_header_layout.addWidget(self.toggle_log_btn)

        self.update_engine_button = QPushButton("🔄 Update yt-dlp")
        self.update_engine_button.setObjectName("microButton")
        self.update_engine_button.setCursor(QCursor(Qt.PointingHandCursor))
        self.update_engine_button.setToolTip("Check and download latest yt-dlp engine release")
        self.update_engine_button.clicked.connect(self.update_engine)
        log_header_layout.addWidget(self.update_engine_button)

        copy_log_button = QPushButton("📋 Copy")
        copy_log_button.setObjectName("microButton")
        copy_log_button.setCursor(QCursor(Qt.PointingHandCursor))
        copy_log_button.clicked.connect(self.copy_log_to_clipboard)
        log_header_layout.addWidget(copy_log_button)

        clear_log_button = QPushButton("🗑 Clear")
        clear_log_button.setObjectName("microButton")
        clear_log_button.setCursor(QCursor(Qt.PointingHandCursor))
        clear_log_button.clicked.connect(self.clear_log)
        log_header_layout.addWidget(clear_log_button)

        log_layout.addLayout(log_header_layout)

        self.log_output = QTextEdit()
        self.log_output.setObjectName("logOutput")
        self.log_output.setReadOnly(True)
        self.log_output.setFont(QFont("Consolas", 9))
        self.log_output.setLineWrapMode(QTextEdit.WidgetWidth)
        self.log_output.setMinimumHeight(70)
        self.log_output.setMaximumHeight(140)
        self.log_output.setVisible(self.is_log_expanded)
        log_layout.addWidget(self.log_output)

        content_layout.addWidget(self.log_card)

        # ----------------- 9. AUTHOR WATERMARK FOOTER -----------------
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 4, 0, 0)
        footer_watermark = QLabel("✨ Crafted with precision by Angelo Golloberda • OmniStream Studio")
        footer_watermark.setObjectName("watermarkLabel")
        footer_watermark.setAlignment(Qt.AlignCenter)
        footer_layout.addWidget(footer_watermark)
        content_layout.addLayout(footer_layout)

        self.scroll_area.setWidget(self.content_widget)
        root_layout.addWidget(self.scroll_area)

        self.reset_metadata_ui()

    def _create_toggle_pill(self, label: str, tooltip: str, checked: bool = False, persist: bool = True) -> QPushButton:
        """Creates a modern checkable studio toggle pill."""
        btn = QPushButton(label)
        btn.setObjectName("togglePill")
        btn.setCheckable(True)
        btn.setChecked(checked)
        btn.setCursor(QCursor(Qt.PointingHandCursor))
        btn.setToolTip(tooltip)
        if persist:
            btn.toggled.connect(self.save_preferences)
        return btn

    def on_trim_toggled(self, checked: bool):
        self.trim_frame.setVisible(checked and self.trim_btn.isEnabled())

    def on_filetype_changed(self, text: str):
        if text and "Waiting" not in text:
            self._preferred_filetype = text
            self.save_preferences()

    # --- Settings Persistence ---
    def restore_preferences(self):
        """Restores saved user preferences across sessions."""
        self.cookie_source_combo.blockSignals(True)
        self.speed_combo.blockSignals(True)
        self.embed_meta_btn.blockSignals(True)
        self.subs_btn.blockSignals(True)
        self.chapters_btn.blockSignals(True)
        self.sponsorblock_btn.blockSignals(True)

        cookie_idx = self.settings.value("cookie_source_idx", 0, type=int)
        if 0 <= cookie_idx < self.cookie_source_combo.count():
            self.cookie_source_combo.setCurrentIndex(cookie_idx)

        cookie_file = self.settings.value("cookie_file_path", "", type=str)
        if not cookie_file or not os.path.isfile(cookie_file):
            local_cookie = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
            if os.path.isfile(local_cookie):
                cookie_file = local_cookie
                if cookie_idx == 0:
                    idx = self.cookie_source_combo.findText("Custom File (cookies.txt)")
                    if idx >= 0:
                        self.cookie_source_combo.setCurrentIndex(idx)

        if cookie_file:
            self.cookie_file_input.setText(cookie_file)
            self.cookie_file_input.setToolTip(cookie_file)

        saved_frags = self.settings.value("concurrent_fragments", 4, type=int)
        frag_idx = self.speed_combo.findData(saved_frags)
        if frag_idx >= 0:
            self.speed_combo.setCurrentIndex(frag_idx)

        self.embed_meta_btn.setChecked(self.settings.value("embed_metadata", True, type=bool))
        self.subs_btn.setChecked(self.settings.value("download_subs", False, type=bool))
        self.chapters_btn.setChecked(self.settings.value("embed_chapters", True, type=bool))
        self.sponsorblock_btn.setChecked(self.settings.value("sponsorblock", False, type=bool))

        self.cookie_source_combo.blockSignals(False)
        self.speed_combo.blockSignals(False)
        self.embed_meta_btn.blockSignals(False)
        self.subs_btn.blockSignals(False)
        self.chapters_btn.blockSignals(False)
        self.sponsorblock_btn.blockSignals(False)

        # Sync cookie UI controls without triggering metadata fetch yet
        is_custom = "Custom File" in self.cookie_source_combo.currentText()
        self.cookie_file_input.setEnabled(is_custom)
        self.cookie_browse_button.setEnabled(is_custom)

    def save_preferences(self, *_args):
        """Persists user preferences via QSettings."""
        self.settings.setValue("output_dir", self.output_dir)
        self.settings.setValue("cookie_source_idx", self.cookie_source_combo.currentIndex())
        self.settings.setValue("cookie_file_path", self.cookie_file_input.text().strip())
        self.settings.setValue("concurrent_fragments", self.speed_combo.currentData() or 4)
        self.settings.setValue("embed_metadata", self.embed_meta_btn.isChecked())
        self.settings.setValue("download_subs", self.subs_btn.isChecked())
        self.settings.setValue("embed_chapters", self.chapters_btn.isChecked())
        self.settings.setValue("sponsorblock", self.sponsorblock_btn.isChecked())
        self.settings.setValue("preferred_filetype", self._preferred_filetype)
        self.settings.setValue("is_log_expanded", self.is_log_expanded)

    def toggle_console(self):
        """Toggles visibility of the activity log console to save vertical space."""
        self.is_log_expanded = not self.is_log_expanded
        self.log_output.setVisible(self.is_log_expanded)
        self.toggle_log_btn.setText("▾ Collapse" if self.is_log_expanded else "▸ Expand")
        self.save_preferences()

    def apply_styles(self):
        """Applies the Modern Studio Dark design system stylesheet."""
        normal_arrow, hover_arrow = ensure_arrow_icons()
        css = """
            /* Global Application Base */
            QMainWindow, QWidget#centralWidget, QWidget#scrollContent {
                background-color: #0B0E14;
                color: #F8FAFC;
                font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, sans-serif;
            }

            QScrollArea#mainScrollArea {
                background: transparent;
                border: none;
            }

            /* Ultra-Sleek Floating Modern Scrollbar */
            QScrollBar:vertical {
                border: none;
                background: transparent;
                width: 6px;
                margin: 4px 2px 4px 0px;
                border-radius: 3px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.16);
                min-height: 35px;
                border-radius: 3px;
            }
            QScrollBar::handle:vertical:hover {
                background: #00DFD8;
            }
            QScrollBar::handle:vertical:pressed {
                background: #00B8B2;
            }
            QScrollBar::sub-line:vertical, QScrollBar::add-line:vertical {
                height: 0px;
                background: none;
                border: none;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
            }

            /* Surface Cards */
            QFrame#surfaceCard {
                background-color: #151823;
                border: 1px solid #232838;
                border-radius: 8px;
            }
            QFrame#subPanel {
                background-color: #0E111A;
                border: 1px solid #242A3D;
                border-radius: 6px;
            }

            /* Header Branding */
            QLabel#logoBadge {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FF0055, stop:0.5 #7928CA, stop:1 #00DFD8);
                color: #FFFFFF;
                font-size: 15px;
                font-weight: bold;
                border-radius: 8px;
            }
            QLabel#appTitleLabel {
                color: #FFFFFF;
                font-size: 13.5pt;
                font-weight: 700;
                letter-spacing: 0.3px;
            }
            QLabel#appSubtitleLabel {
                color: #64748B;
                font-size: 8pt;
            }
            QLabel#statusPill {
                background-color: #1A2234;
                color: #10B981;
                font-size: 8pt;
                font-weight: 600;
                padding: 4px 10px;
                border-radius: 10px;
                border: 1px solid #243048;
            }

            /* Universal Labels */
            QLabel {
                color: #F8FAFC;
            }
            QLabel#boldLabel {
                color: #FFFFFF;
                font-size: 9pt;
                font-weight: 600;
            }
            QLabel#iconLabel {
                font-size: 10.5pt;
            }

            /* Section Headers */
            QLabel#sectionHeaderLabel {
                color: #64748B;
                font-size: 7.5pt;
                font-weight: 700;
                letter-spacing: 0.8px;
            }
            QLabel#hintLabel {
                color: #94A3B8;
                font-size: 8pt;
                font-style: italic;
            }
            QLabel#watermarkLabel {
                color: #64748B;
                font-size: 8pt;
                font-weight: 500;
                letter-spacing: 0.4px;
                padding: 4px;
            }
            QLabel#watermarkLabel:hover {
                color: #00DFD8;
            }

            /* Input Fields */
            QLineEdit#urlInput, QLineEdit#outputDirField {
                background-color: #0E111A;
                color: #F8FAFC;
                border: 1px solid #242A3D;
                border-radius: 6px;
                padding: 5px 10px;
                font-size: 9pt;
            }
            QLineEdit#urlInput:focus {
                border: 1.5px solid #00DFD8;
                background-color: #121622;
            }
            QLineEdit#outputDirField {
                color: #E2E8F0;
                font-size: 9pt;
            }

            /* Buttons */
            QPushButton#actionButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF0055, stop:1 #7928CA);
                color: #FFFFFF;
                font-weight: 600;
                font-size: 9pt;
                border: none;
                border-radius: 6px;
                padding: 6px 14px;
            }
            QPushButton#actionButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF1A6B, stop:1 #8E3EE0);
            }
            QPushButton#actionButton:pressed {
                background: #D90048;
            }

            QPushButton#ghostButton {
                background-color: #1A1F2D;
                color: #E2E8F0;
                font-weight: 500;
                font-size: 8.5pt;
                border: 1px solid #2B3347;
                border-radius: 6px;
                padding: 6px 11px;
            }
            QPushButton#ghostButton:hover {
                background-color: #242B3E;
                border: 1px solid #3E4964;
                color: #FFFFFF;
            }
            QPushButton#ghostButton:pressed {
                background-color: #151A25;
            }
            QPushButton#ghostButton:disabled {
                background-color: #12151F;
                color: #475569;
                border: 1px solid #1C2232;
            }

            /* Checkable Studio Enhancement Pills */
            QPushButton#togglePill {
                background-color: #0E111A;
                color: #94A3B8;
                font-size: 8pt;
                font-weight: 600;
                border: 1px solid #252C40;
                border-radius: 13px;
                padding: 4px 11px;
            }
            QPushButton#togglePill:hover {
                border: 1px solid #3B4664;
                color: #E2E8F0;
            }
            QPushButton#togglePill:checked {
                background-color: #182638;
                color: #00DFD8;
                border: 1px solid #00DFD8;
            }
            QPushButton#togglePill:disabled {
                background-color: #11141E;
                color: #475569;
                border: 1px solid #1B2030;
            }

            QPushButton#microButton {
                background-color: #181D2A;
                color: #94A3B8;
                font-size: 7.5pt;
                font-weight: 500;
                border: 1px solid #273042;
                border-radius: 4px;
                padding: 2px 7px;
            }
            QPushButton#microButton:hover {
                background-color: #242B3E;
                color: #FFFFFF;
            }

            /* Media Preview Area */
            QLabel#thumbnailContainer {
                background-color: #0E111A;
                border: 1px solid #242A3D;
                border-radius: 8px;
                color: #475569;
                font-size: 18pt;
            }
            QLabel#videoTitleLabel {
                color: #FFFFFF;
                font-size: 10.5pt;
                font-weight: 600;
            }
            QLabel#channelBadge {
                color: #00DFD8;
                font-size: 8.5pt;
                font-weight: 500;
            }
            QLabel#metaBadge {
                background-color: #1E2435;
                color: #94A3B8;
                font-size: 8pt;
                font-weight: 500;
                padding: 2px 6px;
                border-radius: 4px;
                border: 1px solid #2A334A;
            }
            QLabel#metaBadgeHighlight {
                background-color: #1A2534;
                color: #00DFD8;
                font-size: 8pt;
                font-weight: 600;
                padding: 2px 6px;
                border-radius: 4px;
                border: 1px solid #233A52;
            }

            /* Custom ComboBox with CSS Vector Arrow */
            QComboBox#customComboBox {
                background-color: #0E111A;
                color: #F8FAFC;
                border: 1px solid #242A3D;
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 9pt;
                min-height: 22px;
            }
            QComboBox#customComboBox:hover {
                border: 1px solid #3B445F;
            }
            QComboBox#customComboBox:focus {
                border: 1px solid #00DFD8;
            }
            QComboBox#customComboBox:disabled {
                background-color: #12151F;
                color: #475569;
                border: 1px solid #1C2232;
            }
            QComboBox#customComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 26px;
                border-left: 1px solid #242A3D;
                border-top-right-radius: 6px;
                border-bottom-right-radius: 6px;
                background: #141824;
            }
            QComboBox#customComboBox::drop-down:hover {
                background: #1C2233;
            }
            QComboBox#customComboBox::down-arrow {
                image: url("{normal_arrow}");
                width: 12px;
                height: 12px;
            }
            QComboBox#customComboBox::down-arrow:hover {
                image: url("{hover_arrow}");
            }
            QComboBox QAbstractItemView {
                background-color: #151823;
                color: #F8FAFC;
                border: 1px solid #2C354A;
                border-radius: 6px;
                selection-background-color: #7928CA;
                selection-color: #FFFFFF;
                padding: 4px;
                outline: 0;
            }

            /* Primary Download Button */
            QPushButton#downloadButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF0055, stop:0.5 #7928CA, stop:1 #00DFD8);
                color: #FFFFFF;
                font-size: 11pt;
                font-weight: 700;
                letter-spacing: 0.5px;
                border: none;
                border-radius: 7px;
            }
            QPushButton#downloadButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF1A6B, stop:0.5 #8E3EE0, stop:1 #20E5DF);
            }
            QPushButton#downloadButton:pressed {
                background: #601DA6;
            }
            QPushButton#downloadButton:disabled {
                background: #1C2130;
                color: #4B556D;
                border: 1px solid #242A3D;
            }

            QPushButton#cancelButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #DC2626, stop:1 #EF4444);
                color: #FFFFFF;
                font-size: 11pt;
                font-weight: 700;
                border: none;
                border-radius: 7px;
            }
            QPushButton#cancelButton:hover {
                background: #B91C1C;
            }

            /* Progress Bar */
            QProgressBar#downloadProgressBar {
                background-color: #0E111A;
                border: 1px solid #202636;
                border-radius: 5px;
                height: 10px;
                text-align: center;
            }
            QProgressBar#downloadProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF0055, stop:0.6 #7928CA, stop:1 #00DFD8);
                border-radius: 4px;
            }

            /* HUD Texts */
            QLabel#hudTextLeft {
                color: #94A3B8;
                font-size: 8.5pt;
                font-weight: 500;
            }
            QLabel#hudTextRight {
                color: #00DFD8;
                font-size: 9pt;
                font-weight: 700;
            }

            /* Console Output */
            QTextEdit#logOutput {
                background-color: #0B0E14;
                color: #94A3B8;
                border: 1px solid #1E2433;
                border-radius: 6px;
                padding: 6px;
                font-family: 'Consolas', monospace;
                font-size: 8.5pt;
            }
        """
        css = css.replace("{normal_arrow}", normal_arrow).replace("{hover_arrow}", hover_arrow)
        self.setStyleSheet(css)

    # --- Cookie Management ---
    def on_cookie_source_changed(self, _index=None):
        """Updates cookie controls and helper hint based on selected source."""
        source = self.cookie_source_combo.currentText()
        is_custom_file = "Custom File" in source
        self.cookie_file_input.setEnabled(is_custom_file)
        self.cookie_browse_button.setEnabled(is_custom_file)

        if "Firefox" in source:
            self.cookie_hint_label.setText(
                "💡 Logged into Webex/University in Mozilla Firefox? Session cookies extract seamlessly even while Firefox is open."
            )
            self.update_log("Auth: Using Firefox session cookies.\n")
        elif any(b in source for b in ("Chrome", "Edge", "Brave")):
            browser_name = source.replace("Browser: ", "")
            self.cookie_hint_label.setText(
                f"💡 Using {browser_name} cookies. Note: If Windows locks the cookie DB while {browser_name} is open, close {browser_name} briefly or use Firefox / cookies.txt."
            )
            self.update_log(f"Auth: Using {browser_name} session cookies.\n")
        elif is_custom_file:
            self.cookie_hint_label.setText(
                "💡 Export cookies from your browser using a 'Get cookies.txt LOCALLY' extension and select the file above."
            )
            self.update_log("Auth: Using custom cookies.txt file.\n")
        else:
            self.cookie_hint_label.setText(
                "💡 Log in to your university portal in your browser, then select that browser above to download."
            )
            self.update_log("Auth: No cookies specified (public access).\n")

        self.save_preferences()
        url = self.url_input.text().strip()
        if self._is_valid_url(url):
            self.schedule_metadata_fetch()

    def choose_cookie_file(self):
        """Prompts user to select a Netscape-formatted cookies.txt file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Cookies File",
            self.output_dir or os.path.expanduser("~"),
            "Cookie Files (*.txt);;All Files (*.*)"
        )
        if file_path:
            self.cookie_file_input.setText(file_path)
            self.cookie_file_input.setToolTip(file_path)
            self.save_preferences()
            self.update_log(f"Selected cookie file: {file_path}\n")
            url = self.url_input.text().strip()
            if self._is_valid_url(url):
                self.schedule_metadata_fetch()

    def get_cookie_args(self):
        """Returns the appropriate CLI cookie flags for yt-dlp."""
        source = self.cookie_source_combo.currentText()
        if "Chrome" in source:
            return ["--cookies-from-browser", "chrome"]
        elif "Edge" in source:
            return ["--cookies-from-browser", "edge"]
        elif "Firefox" in source:
            return ["--cookies-from-browser", "firefox"]
        elif "Brave" in source:
            return ["--cookies-from-browser", "brave"]
        elif "Custom File" in source:
            path = self.cookie_file_input.text().strip()
            if path and os.path.isfile(path):
                return ["--cookies", path]
            elif path:
                self.update_log(f"Warning: Cookie file '{path}' not found.\n")
        return []

    # --- UI Actions ---
    def paste_and_fetch(self):
        """Pastes from clipboard and immediately begins metadata fetch."""
        clipboard = QApplication.clipboard()
        text = clipboard.text().strip()
        if text:
            self.metadata_fetch_timer.stop()
            self.url_input.blockSignals(True)
            self.url_input.setText(text)
            self.url_input.blockSignals(False)
            self.fetch_metadata()

    def clear_url_input(self):
        """Clears URL input, cancels any active metadata fetch, and resets UI states."""
        self.metadata_fetch_timer.stop()
        self._cancel_pending_fetches()
        self.url_input.blockSignals(True)
        self.url_input.clear()
        self.url_input.blockSignals(False)
        self.custom_filename_input.clear()
        self.reset_metadata_ui()
        self.set_ui_loading_state(False)
        self.set_status_pill("● Ready", "#10B981", "#1A2234")

    def choose_output_folder(self):
        """Allows user to pick target folder via file dialog."""
        start_dir = self.output_dir or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Select Download Directory", start_dir)
        if folder:
            self.output_dir = folder
            self.output_dir_label.setText(folder)
            self.output_dir_label.setToolTip(folder)
            self.save_preferences()
            self.update_log(f"Target folder set to: {folder}\n")

    def play_last_file(self):
        """Opens the most recently downloaded media file in the user's default player."""
        if self.last_downloaded_file and os.path.isfile(self.last_downloaded_file):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_downloaded_file))
        else:
            QMessageBox.information(self, "File Not Available", "The downloaded file could not be located on disk.")
            self.play_file_button.setEnabled(False)

    def open_output_folder(self):
        """Reveals the downloaded file (if present) or opens the target directory in Windows Explorer."""
        if sys.platform == "win32" and self.last_downloaded_file and os.path.isfile(self.last_downloaded_file):
            try:
                norm_path = os.path.normpath(self.last_downloaded_file)
                subprocess.Popen(["explorer.exe", f"/select,{norm_path}"])
                return
            except Exception:
                pass

        if self.output_dir and os.path.isdir(self.output_dir):
            try:
                os.startfile(self.output_dir)
            except Exception:
                QDesktopServices.openUrl(QUrl.fromLocalFile(self.output_dir))
        else:
            QMessageBox.warning(self, "Folder Not Found", "The selected destination folder does not exist.")

    def schedule_metadata_fetch(self):
        """Debounced fetch trigger when user types in URL input."""
        text = self.url_input.text().strip()
        if not text:
            self._cancel_pending_fetches()
            self.reset_metadata_ui()
            self.set_ui_loading_state(False)
            self.set_status_pill("● Ready", "#10B981", "#1A2234")
            return
        self.set_status_pill("● Checking...", "#00DFD8", "#1A2738")
        self.metadata_fetch_timer.start(450)

    def _is_valid_url(self, url: str) -> bool:
        return bool(url) and not url.isspace() and (
            "http://" in url or "https://" in url or "webex" in url or "youtu" in url
        )

    def _cancel_pending_fetches(self):
        """Aborts any in-flight metadata or thumbnail threads without blocking the main UI thread."""
        self._active_request_id += 1
        if self.metadata_thread and self.metadata_thread.isRunning():
            self.metadata_thread.abort()
            self._retire_thread(self.metadata_thread)
        self.metadata_thread = None

        if self.thumbnail_thread and self.thumbnail_thread.isRunning():
            self.thumbnail_thread.abort()
            self._retire_thread(self.thumbnail_thread)
        self.thumbnail_thread = None

    # --- Metadata Handling ---
    def fetch_metadata(self):
        """Starts fetching video metadata on a cancellable background thread."""
        self.metadata_fetch_timer.stop()
        url = self.url_input.text().strip()
        if not url:
            return

        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
            self.url_input.blockSignals(True)
            self.url_input.setText(url)
            self.url_input.blockSignals(False)

        if not self._is_valid_url(url):
            self.set_status_pill("● Invalid Link", "#F59E0B", "#2A2318")
            self.status_label.setText("Invalid URL")
            self.update_log(f"Notice: '{url}' is not recognized as a valid video or stream URL.\n")
            return

        ytdlp_bin = get_ytdlp_executable()
        if not os.path.isfile(ytdlp_bin) and not shutil.which(ytdlp_bin):
            self.set_status_pill("● Setup Required", "#F59E0B", "#2A2318")
            self.status_label.setText("Engine Setup Required")
            self.update_log("Notice: yt-dlp engine is required. Run 'winget install yt-dlp' or download from GitHub.\n")
            self.show_engine_setup_dialog(missing_ytdlp=True, missing_ffmpeg=not has_ffmpeg())
            return

        self._cancel_pending_fetches()
        req_id = self._active_request_id

        self.reset_metadata_ui()
        self.set_ui_loading_state(True, "Loading metadata...")
        self.progress_bar.setRange(0, 0)  # Smooth indeterminate loading pulse
        self.set_status_pill("● Fetching Info", "#00DFD8", "#1A2738")
        self.update_log(f"Fetching stream details: {url} ...\n")

        cookie_args = self.get_cookie_args()
        self.metadata_thread = MetadataFetcherThread(req_id, url, self.metadata_signal, cookie_args=cookie_args)
        self.metadata_thread.start()

    def _populate_filetype_combo(self, is_playlist: bool = False):
        """Populates media container / audio formats and restores user's preferred selection."""
        self.filetype_combo.blockSignals(True)
        self.filetype_combo.clear()
        items = [
            "Best Available",
            "MP4 (Universal Video)",
            "MKV (High Definition)",
            "WebM (Modern Web)",
            "---",
            "Audio Only (MP3 - High Quality)",
            "Audio Only (M4A - Apple AAC)",
            "Audio Only (FLAC - Lossless)",
            "Audio Only (OPUS)",
            "Audio Only (WAV - Uncompressed)",
        ]
        for item in items:
            if item == "---":
                self.filetype_combo.insertSeparator(self.filetype_combo.count())
            else:
                self.filetype_combo.addItem(item)

        idx = self.filetype_combo.findText(self._preferred_filetype)
        if idx >= 0:
            self.filetype_combo.setCurrentIndex(idx)
        self.filetype_combo.blockSignals(False)
        self.filetype_combo.setEnabled(True)

    def _sync_trim_availability(self):
        """Enables clip trimming only when downloading a single stream/video."""
        is_multi_playlist = (
            self.playlist_scope_combo.isVisible()
            and self.playlist_scope_combo.currentData() != "single"
        )
        self.trim_btn.setEnabled(not is_multi_playlist)
        if is_multi_playlist:
            self.trim_btn.setToolTip("Switch Download Scope to 'This Single Video Only' to trim clip timestamps")
            self.trim_frame.setVisible(False)
        else:
            self.trim_btn.setToolTip(
                "Download only a specific timestamp range (e.g. 00:10:00 to 00:25:30) without downloading the full stream"
            )
            self.trim_frame.setVisible(self.trim_btn.isChecked())

    def on_playlist_scope_changed(self, index):
        """Updates download button text and clip trimmer state based on playlist vs single scope."""
        if not self.video_info:
            return
        is_playlist = (self.video_info.get('_type') == 'playlist') or ('entries' in self.video_info)
        if is_playlist:
            user_data = self.playlist_scope_combo.itemData(index)
            raw_entries = self.video_info.get('entries', [])
            playlist_count = self.video_info.get('playlist_count')
            if playlist_count and playlist_count > len(raw_entries):
                count = playlist_count
            else:
                count = len(raw_entries) if raw_entries else (playlist_count or 0)
            count_str = f"{count:,}"
            if user_data == "single":
                self.download_button.setText("⚡ Download Single Video")
                self.custom_filename_input.setPlaceholderText("Optional custom video filename (leave blank for original title)...")
            elif user_data == "all":
                self.download_button.setText(f"⚡ Download Complete Mix ({count_str} Videos)")
                self.custom_filename_input.setPlaceholderText("Optional custom playlist folder name (leave blank for playlist title)...")
            else:
                self.download_button.setText(f"⚡ Download First {user_data} Videos")
                self.custom_filename_input.setPlaceholderText("Optional custom playlist folder name (leave blank for playlist title)...")
        self._sync_trim_availability()

    def on_metadata_ready(self, request_id: int, video_info: dict):
        """Populates UI cards with fetched metadata if request_id matches current request."""
        if request_id != self._active_request_id:
            return

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        current_input = self.url_input.text().strip()
        if not current_input:
            return

        self.video_info = video_info
        is_playlist = (self.video_info.get('_type') == 'playlist') or ('entries' in self.video_info)

        if is_playlist:
            raw_entries = self.video_info.get('entries', [])
            entries = [e for e in raw_entries if e]
            playlist_count = self.video_info.get('playlist_count')
            if playlist_count and playlist_count > len(entries):
                count = playlist_count
            else:
                count = len(entries) if entries else (playlist_count or 0)
            count_str = f"{count:,}"
            raw_title = self.video_info.get("title") or f"Mix / Playlist ({count_str} Videos)"
            clean_title = raw_title.strip()
            self.video_title_label.setText(f"📚 {clean_title}")
            self.custom_filename_input.setPlaceholderText("Optional custom playlist folder name (leave blank for playlist title)...")
            self.update_log(f"Loaded Playlist/Mix: {clean_title} ({count_str} items found)\n")

            self.channel_badge.setText(f"📁 Playlist ({count_str} items)")
            self.duration_badge.setText("⏱ Multiple Tracks")
            self.views_badge.setText(f"🔢 {count_str} Videos")

            self.status_label.setText("Playlist Ready")
            self.set_status_pill("● Playlist Ready", "#10B981", "#1A2234")

            self.playlist_scope_label.setVisible(True)
            self.playlist_scope_combo.setVisible(True)
            self.playlist_scope_combo.blockSignals(True)
            self.playlist_scope_combo.clear()
            self.playlist_scope_combo.addItem(f"📚 Entire Mix / Playlist (All {count_str} items)", "all")
            if count > 25:
                self.playlist_scope_combo.addItem("🎵 First 25 Videos", 25)
            if count > 50:
                self.playlist_scope_combo.addItem("🎵 First 50 Videos", 50)
            if count > 100:
                self.playlist_scope_combo.addItem("🎵 First 100 Videos", 100)
            self.playlist_scope_combo.addItem("🎬 This Single Video Only", "single")
            self.playlist_scope_combo.blockSignals(False)

            thumbnail_url = self.video_info.get('thumbnail')
            if not thumbnail_url and entries:
                first_entry = entries[0]
                thumbnail_url = first_entry.get('thumbnail')
                if not thumbnail_url and first_entry.get('id'):
                    thumbnail_url = f"https://i.ytimg.com/vi/{first_entry.get('id')}/hqdefault.jpg"

            if thumbnail_url:
                self.thumbnail_thread = ThumbnailLoaderThread(request_id, thumbnail_url, self.thumbnail_signal)
                self.thumbnail_thread.start()

            self.quality_data.clear()
            self.quality_combo.clear()
            self.quality_combo.addItem("Best Available (Highest Resolution)")
            self.quality_data["Best Available (Highest Resolution)"] = "Best Available"
            for q in ["2160p", "1440p", "1080p", "720p", "480p", "360p"]:
                display_q = f"{q} (If Available)"
                self.quality_combo.addItem(display_q)
                self.quality_data[display_q] = q

            self.quality_combo.setEnabled(True)
            self._populate_filetype_combo(is_playlist=True)

            self.download_button.setText(f"⚡ Download Complete Mix ({count_str} Videos)")
            self.download_button.setEnabled(True)
            self.set_ui_loading_state(False)
            self._sync_trim_availability()
            return

        # Single video handling
        self.playlist_scope_label.setVisible(False)
        self.playlist_scope_combo.setVisible(False)
        self.download_button.setText("⚡ Start Download")
        self.custom_filename_input.setPlaceholderText("Optional custom filename (leave blank to use original title)...")

        title = self.video_info.get("title", "Unknown Stream / Recording")
        self.video_title_label.setText(title)
        self.update_log(f"Loaded: {title}\n")

        channel = self.video_info.get("uploader") or self.video_info.get("channel") or "Media Stream"
        self.channel_badge.setText(f"👤 {channel}")

        duration_sec = self.video_info.get("duration")
        self.duration_badge.setText(f"⏱ {format_duration(duration_sec)}")

        views_count = self.video_info.get("view_count")
        self.views_badge.setText(f"👁 {format_views(views_count)}" if views_count else "🔒 Private/Stream")

        self.status_label.setText("Metadata Ready")
        self.set_status_pill("● Ready to Download", "#10B981", "#1A2234")

        thumbnail_url = self.video_info.get('thumbnail')
        if thumbnail_url:
            self.thumbnail_thread = ThumbnailLoaderThread(request_id, thumbnail_url, self.thumbnail_signal)
            self.thumbnail_thread.start()

        formats = self.video_info.get("formats", [])
        self.quality_data.clear()
        self.quality_combo.clear()
        self.quality_combo.addItem("Best Available (Highest Resolution)")
        self.quality_data["Best Available (Highest Resolution)"] = "Best Available"

        if not formats:
            self.quality_combo.setEnabled(True)
            self._populate_filetype_combo(is_playlist=False)
            self.download_button.setEnabled(True)
            self.set_ui_loading_state(False)
            self._sync_trim_availability()
            self.update_log("Notice: Best Available format stream will be used.\n")
            return

        best_audio_size = 0
        best_audio_abr = 0
        for f in formats:
            if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                abr = f.get('abr', 0) or 0
                fsize = f.get('filesize') or f.get('filesize_approx') or 0
                if abr > best_audio_abr and fsize:
                    best_audio_abr = abr
                    best_audio_size = fsize
                elif not best_audio_size and fsize:
                    best_audio_size = fsize

        unique_heights = sorted(
            list(set(f['height'] for f in formats if f.get('height') and f.get('vcodec') != 'none')),
            reverse=True
        )
        for h in unique_heights:
            best_video_size_for_h = 0
            best_video_metric = 0
            for f in formats:
                if f.get('height') == h and f.get('vcodec') != 'none':
                    fsize = f.get('filesize') or f.get('filesize_approx') or 0
                    if fsize:
                        metric = f.get('vbr', 0) or f.get('tbr', 0) or (f.get('width', 0) * f.get('height', 0))
                        if metric > best_video_metric:
                            best_video_metric = metric
                            best_video_size_for_h = fsize
                        elif not best_video_size_for_h:
                            best_video_size_for_h = fsize

            total_approx = (best_video_size_for_h or 0) + (best_audio_size or 0)
            quality_val = f"{h}p"
            size_str = f" (~{format_bytes(total_approx)})" if total_approx > 0 else ""
            display_text = f"{quality_val}{size_str}"
            self.quality_combo.addItem(display_text)
            self.quality_data[display_text] = quality_val

        self.quality_combo.setEnabled(True)
        self._populate_filetype_combo(is_playlist=False)

        self.download_button.setEnabled(True)
        self.set_ui_loading_state(False)
        self._sync_trim_availability()

    def on_thumbnail_ready(self, request_id: int, img_data: bytes):
        """Renders fetched thumbnail with rounded antialiased corners."""
        if request_id != self._active_request_id:
            return
        try:
            image = QImage()
            if image.loadFromData(img_data):
                pixmap = QPixmap.fromImage(image)
                target_w = self.thumbnail_label.width()
                target_h = self.thumbnail_label.height()
                scaled = pixmap.scaled(target_w, target_h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)

                x = (scaled.width() - target_w) // 2
                y = (scaled.height() - target_h) // 2
                cropped = scaled.copy(x, y, target_w, target_h)

                rounded = QPixmap(target_w, target_h)
                rounded.fill(Qt.transparent)
                painter = QPainter(rounded)
                painter.setRenderHint(QPainter.Antialiasing)
                path = QPainterPath()
                path.addRoundedRect(QRectF(0, 0, target_w, target_h), 6, 6)
                painter.setClipPath(path)
                painter.drawPixmap(0, 0, cropped)
                painter.end()

                self.thumbnail_label.setText("")
                self.thumbnail_label.setPixmap(rounded)
        except Exception as e:
            self.update_log(f"Thumbnail render warning: {e}\n")

    def on_thumbnail_error(self, request_id: int, _err_msg: str):
        if request_id == self._active_request_id:
            self.thumbnail_label.setText("🎬")

    def on_metadata_error(self, request_id: int, error_message: str):
        """Handles errors during metadata fetch with clear visual indicators and actionable recovery steps."""
        if request_id != self._active_request_id:
            return

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.update_log(f"Metadata Error: {error_message}\n")

        self.reset_metadata_ui(keep_status=True)
        self.set_ui_loading_state(False)

        low_err = error_message.lower()
        if "could not copy chrome cookie" in low_err or "permission denied" in low_err or "dpapi" in low_err:
            self.status_label.setText("Browser Cookie DB Locked")
            self.set_status_pill("● Cookie Locked", "#F59E0B", "#2A2012")
            self.video_title_label.setText("Browser Cookie Database Locked")
            self.channel_badge.setText("Close Chrome/Edge briefly or switch to Firefox / cookies.txt")
            self.update_log(
                "💡 Tip: Chromium browsers lock their cookie database while open on Windows.\n"
                "   Close Chrome/Edge briefly, switch to Mozilla Firefox, or export a cookies.txt file.\n"
            )
        elif any(k in low_err for k in ("bot", "sign in", "login", "authentication", "private", "cookies", "confirm you")):
            self.status_label.setText("Sign In / Cookies Required")
            self.set_status_pill("● Auth Required", "#F59E0B", "#2A2012")
            self.video_title_label.setText("YouTube Sign-in / Bot Verification Required")
            self.channel_badge.setText("Select your browser session in 'Authentication & Cookies' above")
            self.update_log(
                "💡 YouTube Bot / Sign-In Check: YouTube requires session verification for this stream.\n"
                "   👉 In '🍪 AUTHENTICATION & COOKIES' above, select your browser (e.g. Chrome, Edge, Firefox) "
                "or choose a cookies.txt file, then fetch again.\n"
            )
            if hasattr(self, "cookie_source_combo"):
                self.cookie_source_combo.setFocus()
        else:
            self.status_label.setText("Error loading metadata")
            self.set_status_pill("● Fetch Error", "#EF4444", "#2A1818")
            self.video_title_label.setText("Unable to load stream details")
            self.channel_badge.setText(error_message[:95] if error_message else "Check activity log for details")

        if "not found in PATH" in error_message:
            QMessageBox.critical(self, "yt-dlp Missing", error_message)

    def reset_metadata_ui(self, keep_status: bool = False):
        """Resets video cards to clean default state."""
        self.video_info = {}
        self.quality_data.clear()
        self.video_title_label.setText("Paste a video link above")
        self.channel_badge.setText("Waiting for URL input...")
        self.duration_badge.setText("--:--")
        self.views_badge.setText("0 views")
        if not keep_status:
            self.status_label.setText("Ready")

        self.playlist_scope_label.setVisible(False)
        self.playlist_scope_combo.setVisible(False)

        self.thumbnail_label.setPixmap(QPixmap())
        self.thumbnail_label.setText("▶")

        self.quality_combo.clear()
        self.quality_combo.addItem("Waiting for video...")
        self.quality_combo.setEnabled(False)

        self.filetype_combo.blockSignals(True)
        self.filetype_combo.clear()
        self.filetype_combo.addItem("Waiting for video...")
        self.filetype_combo.blockSignals(False)
        self.filetype_combo.setEnabled(False)

        self.download_button.setEnabled(False)
        self.download_button.setText("⬇  Start Download")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.hud_speed_size.setText("⚡ Ready to download")
        self.hud_eta_percent.setText("0%")

    def set_ui_loading_state(self, loading: bool, status_text: str = ""):
        """Controls enabling of interactive controls during metadata fetch (keeps URL & Clear responsive!)."""
        is_ready = not loading and bool(self.video_info)
        self.paste_button.setEnabled(not loading)
        self.choose_dir_button.setEnabled(not loading)
        self.open_folder_button.setEnabled(not loading)
        self.cookie_source_combo.setEnabled(not loading)
        self.cookie_browse_button.setEnabled(not loading and ("Custom File" in self.cookie_source_combo.currentText()))
        self.quality_combo.setEnabled(is_ready)
        self.filetype_combo.setEnabled(is_ready)
        self.download_button.setEnabled(is_ready)
        if loading and status_text:
            self.status_label.setText(status_text)
            self.hud_speed_size.setText("🔍 Fetching stream metadata...")
            self.hud_eta_percent.setText("...")

    def set_status_pill(self, text: str, fg: str, bg: str):
        """Updates the header status badge."""
        self.status_pill.setText(text)
        self.status_pill.setStyleSheet(
            f"background-color: {bg}; color: {fg}; font-size: 8pt; font-weight: 600; "
            f"padding: 4px 10px; border-radius: 10px; border: 1px solid {fg}40;"
        )

    def _validate_clip_section(self) -> Tuple[bool, Optional[str]]:
        """Validates clip trim fields and returns (is_valid, 'start-end' or None)."""
        if not (self.trim_btn.isEnabled() and self.trim_btn.isChecked()):
            return True, None

        if not has_ffmpeg():
            QMessageBox.warning(
                self, "FFmpeg Required for Clip Trimming",
                "Trimming clip timestamps requires FFmpeg.\n\n"
                "Install it in PowerShell via:\nwinget install Gyan.FFmpeg"
            )
            return False, None

        raw_start = self.trim_start_input.text().strip()
        raw_end = self.trim_end_input.text().strip()
        if not raw_start and not raw_end:
            QMessageBox.warning(
                self, "Clip Timestamps Empty",
                "Enter a Start timestamp (e.g. 00:02:00) or End timestamp (e.g. 00:08:30), or turn off 'Trim Clip'."
            )
            return False, None

        start_sec = 0.0
        if raw_start:
            parsed_start = parse_timestamp_seconds(raw_start)
            if parsed_start is None:
                QMessageBox.warning(
                    self, "Invalid Start Time",
                    f"'{raw_start}' is not a valid timestamp.\nUse MM:SS or HH:MM:SS (e.g. 02:30 or 00:15:00)."
                )
                return False, None
            start_sec = parsed_start

        end_str = "inf"
        if raw_end and raw_end.lower() != "inf":
            end_sec = parse_timestamp_seconds(raw_end)
            if end_sec is None:
                QMessageBox.warning(
                    self, "Invalid End Time",
                    f"'{raw_end}' is not a valid timestamp.\nUse MM:SS or HH:MM:SS, or leave blank for end of stream."
                )
                return False, None
            if end_sec <= start_sec:
                QMessageBox.warning(
                    self, "Invalid Clip Range",
                    "End timestamp must be greater than Start timestamp."
                )
                return False, None
            end_str = seconds_to_ytdlp_ts(end_sec)

        return True, f"{seconds_to_ytdlp_ts(start_sec)}-{end_str}"

    # --- Download Handling ---
    def handle_download_button_click(self):
        """Handles click on Start / Cancel download button."""
        if self.download_thread and self.download_thread.isRunning():
            self.download_thread.stop()
            self.set_status_pill("● Cancelling...", "#F59E0B", "#2A2318")
            self.hud_speed_size.setText("⏹ Cancelling download & cleaning up processes...")
            return
        self.start_download()

    def start_download(self):
        """Validates inputs and launches DownloadThread."""
        if not self.output_dir or not os.path.isdir(self.output_dir):
            QMessageBox.warning(
                self, "Output Directory Required",
                "Please select a valid destination folder using 'Browse...'."
            )
            return

        url = self.url_input.text().strip()
        if not self._is_valid_url(url) or not self.video_info:
            QMessageBox.warning(self, "Metadata Required", "Please load a valid video URL before downloading.")
            return

        clip_ok, clip_section = self._validate_clip_section()
        if not clip_ok:
            return

        quality_display = self.quality_combo.currentText()
        quality = self.quality_data.get(quality_display, "Best Available")
        filetype = self.filetype_combo.currentText()
        cookie_args = self.get_cookie_args()
        fragments = self.speed_combo.currentData() or 4
        embed_meta = self.embed_meta_btn.isChecked()
        dl_subs = self.subs_btn.isChecked()
        sub_langs = resolve_subtitle_langs(self.video_info)
        embed_chaps = self.chapters_btn.isChecked()
        use_sb = self.sponsorblock_btn.isChecked()
        custom_name = self.custom_filename_input.text().strip()

        is_playlist = False
        playlist_end = None
        playlist_items = None
        if self.playlist_scope_combo.isVisible():
            scope_mode = self.playlist_scope_combo.currentData()
            if scope_mode != "single":
                is_playlist = True
                if isinstance(scope_mode, int):
                    playlist_end = scope_mode
            else:
                # If URL is a pure playlist URL without v= video parameter, fetch item 1 safely
                if "list=" in url and "v=" not in url and "youtu.be/" not in url:
                    playlist_items = "1"

        self.last_downloaded_file = ""
        self.play_file_button.setEnabled(False)
        self.set_ui_downloading_state(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.set_status_pill("● Downloading", "#FF0055", "#2A1820")
        self.hud_speed_size.setText("⚡ Initializing stream...")
        self.hud_eta_percent.setText("0%")

        enhancements_summary = []
        if embed_meta:
            enhancements_summary.append("Metadata+Art")
        if dl_subs:
            enhancements_summary.append(f"Subs({sub_langs})")
        if embed_chaps:
            enhancements_summary.append("Chapters")
        if use_sb:
            enhancements_summary.append("SponsorBlock")
        if clip_section:
            enhancements_summary.append(f"Trim({clip_section})")

        cookie_display = ' '.join(cookie_args) if cookie_args else 'None'
        self.update_log(
            f"--- Starting Download ---\n"
            f"Title: {self.video_info.get('title')}\n"
            f"Quality: {quality} | Format: {filetype} | Speed: {fragments}x fragments\n"
            f"Enhancements: {', '.join(enhancements_summary) if enhancements_summary else 'Standard'}\n"
            f"Folder: {self.output_dir}\n"
            f"Cookies: {cookie_display}\n"
        )

        self.download_thread = DownloadThread(
            url=url,
            quality=quality,
            filetype=filetype,
            output_signal=self.output_signal,
            output_path=self.output_dir,
            cookie_args=cookie_args,
            is_playlist=is_playlist,
            playlist_end=playlist_end,
            playlist_items=playlist_items,
            concurrent_fragments=fragments,
            embed_metadata=embed_meta,
            download_subs=dl_subs,
            sub_langs=sub_langs,
            embed_chapters=embed_chaps,
            sponsorblock=use_sb,
            clip_section=clip_section,
            custom_name=custom_name
        )
        self.download_thread.start()

    def set_ui_downloading_state(self, downloading: bool):
        """Adapts UI states for active download or completion."""
        enable = not downloading
        self.choose_dir_button.setEnabled(enable)
        self.open_folder_button.setEnabled(enable)
        self.url_input.setEnabled(enable)
        self.paste_button.setEnabled(enable)
        self.clear_button.setEnabled(enable)
        self.custom_filename_input.setEnabled(enable)
        self.cookie_source_combo.setEnabled(enable)
        self.cookie_browse_button.setEnabled(enable and ("Custom File" in self.cookie_source_combo.currentText()))
        self.quality_combo.setEnabled(enable and bool(self.video_info))
        self.filetype_combo.setEnabled(enable and bool(self.video_info))
        self.speed_combo.setEnabled(enable)
        self.embed_meta_btn.setEnabled(enable)
        self.subs_btn.setEnabled(enable)
        self.chapters_btn.setEnabled(enable)
        self.sponsorblock_btn.setEnabled(enable)
        self.trim_start_input.setEnabled(enable)
        self.trim_end_input.setEnabled(enable)
        if hasattr(self, 'playlist_scope_combo'):
            self.playlist_scope_combo.setEnabled(enable)
        if enable:
            self._sync_trim_availability()
        else:
            self.trim_btn.setEnabled(False)

        if downloading:
            self.download_button.setText("⏹  Cancel Download")
            self.download_button.setObjectName("cancelButton")
            self.download_button.setStyleSheet("""
                QPushButton#cancelButton {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #DC2626, stop:1 #EF4444);
                    color: #FFFFFF; font-size: 11pt; font-weight: 700; border-radius: 7px;
                }
                QPushButton#cancelButton:hover { background: #B91C1C; }
            """)
        else:
            self.download_button.setObjectName("downloadButton")
            self.download_button.setStyleSheet("")  # Reverts to base stylesheet
            if self.playlist_scope_combo.isVisible():
                self.on_playlist_scope_changed(self.playlist_scope_combo.currentIndex())
            else:
                self.download_button.setText("⬇  Start Download")

    def on_file_produced(self, filepath: str):
        """Records exact final file path produced by yt-dlp for Play & Reveal actions."""
        if filepath and os.path.isfile(filepath):
            self.last_downloaded_file = filepath

    def on_download_complete(self, success: bool, message: str):
        """Handles post-download UI updates."""
        self.set_ui_downloading_state(False)
        self.setWindowTitle(APP_NAME)
        self.update_log(f"Result: {message}\n")

        if success:
            self.progress_bar.setValue(100)
            if self.last_downloaded_file and os.path.isfile(self.last_downloaded_file):
                self.play_file_button.setEnabled(True)
            if "skipped" in message.lower():
                self.set_status_pill("● Completed (with skips)", "#10B981", "#1A2234")
                self.hud_speed_size.setText(f"✅ {message} Saved to target folder.")
            else:
                self.set_status_pill("● Completed!", "#10B981", "#1A2234")
                self.hud_speed_size.setText("✅ Download complete! Click '▶ Play' or 'Reveal' above.")
            self.hud_eta_percent.setText("100%")
        else:
            self.set_status_pill("● Download Error", "#EF4444", "#2A1818")
            self.hud_speed_size.setText(f"❌ {message}")

        self.download_thread = None

    def update_log(self, text: str):
        """Appends text to the terminal console."""
        self.log_output.append(text.strip())
        self.log_output.verticalScrollBar().setValue(self.log_output.verticalScrollBar().maximum())

    def update_progress(self, percent: int):
        """Updates progress bar, percentage HUD, and taskbar window title."""
        self.progress_bar.setValue(percent)
        self.hud_eta_percent.setText(f"{percent}%")
        if self.download_thread and self.download_thread.isRunning():
            self.setWindowTitle(f"[{percent}%] {APP_NAME}")

    def update_stage(self, stage_text: str):
        """Displays live FFmpeg post-processing stages (merging, audio extraction, subtitles, etc.)."""
        self.hud_speed_size.setText(stage_text)

    def update_stats(self, speed: str, eta: str, size: str):
        """Updates download HUD with live speed, size, and ETA."""
        parts = []
        if speed:
            parts.append(f"⚡ {speed}")
        if size:
            parts.append(f"📦 {size}")
        if parts:
            self.hud_speed_size.setText("  •  ".join(parts))
        if eta:
            current_pct = self.progress_bar.value()
            self.hud_eta_percent.setText(f"{current_pct}%  (⏳ {eta})")

    def copy_log_to_clipboard(self):
        """Copies terminal console content to clipboard."""
        QApplication.clipboard().setText(self.log_output.toPlainText())
        self.hud_speed_size.setText("📋 Log copied to clipboard!")

    def clear_log(self):
        """Clears the terminal console."""
        self.log_output.clear()

    def update_engine(self):
        """Launches in-app yt-dlp -U update check on background thread."""
        self.update_engine_button.setEnabled(False)
        self.update_engine_button.setText("🔄 Checking...")
        self.set_status_pill("● Checking Updates", "#00DFD8", "#1A2738")
        self.engine_update_thread = EngineUpdateThread()
        self.engine_update_thread.output_received.connect(self.update_log)
        self.engine_update_thread.update_finished.connect(self.on_engine_update_finished)
        self.engine_update_thread.start()

    def on_engine_update_finished(self, success: bool, message: str):
        self.update_engine_button.setEnabled(True)
        self.update_engine_button.setText("🔄 Update yt-dlp")
        if success:
            self.set_status_pill("● Engine Ready", "#10B981", "#1A2234")
            self.update_log(f"Status: {message}\n")
        else:
            self.set_status_pill("● Update Finished", "#94A3B8", "#1A2234")
            self.update_log(f"Notice: {message}\n")

    # --- Window Closing ---
    def closeEvent(self, event):
        self.save_preferences()
        self.metadata_fetch_timer.stop()
        self._cancel_pending_fetches()

        if self.download_thread and self.download_thread.isRunning():
            reply = QMessageBox.question(
                self, 'Confirm Exit',
                "A download is currently in progress. Stop and exit?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.download_thread.stop()
                self.download_thread.wait(1500)
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


# --- Main Execution ---
if __name__ == "__main__":
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
