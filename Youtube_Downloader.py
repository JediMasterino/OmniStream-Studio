# -*- coding: utf-8 -*-
import sys
import subprocess
import threading
import json
import re
import os
import shutil
import ssl
from urllib.request import urlopen
from urllib.error import HTTPError, URLError
from socket import timeout as TimeoutError

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLineEdit, QPushButton, QTextEdit, QLabel, QComboBox, QSizePolicy,
    QFileDialog, QProgressBar, QMessageBox, QDesktopWidget, QFrame, QScrollArea
)
from PyQt5.QtCore import (
    pyqtSignal, QObject, Qt, QThread, QTimer, QRectF, QUrl, QPointF
)
from PyQt5.QtGui import (
    QPixmap, QFont, QColor, QImage, QPainter, QPainterPath,
    QDesktopServices, QCursor, QPen
)

# --- Constants ---
# Low minimum bounds so users on any monitor or laptop scaling can resize freely
MIN_WINDOW_WIDTH = 640
MIN_WINDOW_HEIGHT = 500
DEFAULT_WINDOW_WIDTH = 720
DEFAULT_WINDOW_HEIGHT = 720
APP_NAME = "OmniStream Studio | by Angelo Golloberda"

# --- Ensure Local / Bundled Binaries (yt-dlp, ffmpeg) are discovered in PATH ---
_app_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
_exe_dir = os.path.dirname(sys.executable)

def _setup_binary_environment():
    """Locates yt-dlp and ffmpeg in application / executable folders and injects them into PATH."""
    found_dirs = []
    ffmpeg_dir = None

    search_roots = [_exe_dir, _app_dir]
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
    """Returns the absolute path to the local or system yt-dlp binary."""
    # 1. Next to the running executable (e.g. portable bundle folder)
    exe_dir = os.path.dirname(sys.executable)
    p_exe = os.path.join(exe_dir, "yt-dlp.exe")
    if os.path.isfile(p_exe):
        return p_exe

    # 2. Bundled PyInstaller temp folder (_MEIPASS)
    app_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    p_app = os.path.join(app_dir, "yt-dlp.exe")
    if os.path.isfile(p_app):
        return p_app

    # 3. Development project directory
    base_dir = os.path.dirname(os.path.abspath(__file__))
    p_base = os.path.join(base_dir, "yt-dlp.exe")
    if os.path.isfile(p_base):
        return p_base

    # 4. System PATH
    p_path = shutil.which("yt-dlp")
    if p_path:
        return p_path

    return "yt-dlp"



# --- Helper Functions ---
def format_bytes(size_bytes):
    """Converts bytes to a human-readable string (KB, MB, GB)."""
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


# --- Signals ---
class OutputSignal(QObject):
    output_received = pyqtSignal(str)
    progress_updated = pyqtSignal(int)
    stats_updated = pyqtSignal(str, str, str)  # speed, eta, size
    download_finished = pyqtSignal(bool, str)


class MetadataSignal(QObject):
    metadata_ready = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)


class ThumbnailSignal(QObject):
    thumbnail_ready = pyqtSignal(bytes)
    error_occurred = pyqtSignal(str)


# --- Threads ---
class ThumbnailLoaderThread(QThread):
    """Fetches thumbnail in background to avoid any UI stutter."""
    def __init__(self, url: str, signal: ThumbnailSignal):
        super().__init__()
        self.url = url
        self.signal = signal

    def run(self):
        try:
            context = ssl._create_unverified_context()
            with urlopen(self.url, context=context, timeout=12) as response:
                data = response.read()
            self.signal.thumbnail_ready.emit(data)
        except Exception as e:
            self.signal.error_occurred.emit(str(e))


class DownloadThread(QThread):
    """Handles the yt-dlp download process in a separate thread with playlist/mix support."""
    def __init__(self, url: str, quality: str, filetype: str, output_signal: OutputSignal, output_path: str, cookie_args=None, is_playlist=False, playlist_end=None):
        super().__init__()
        self.url = url
        self.quality = quality
        self.filetype = filetype
        self.output_signal = output_signal
        self.output_path = output_path
        self.cookie_args = cookie_args or []
        self.is_playlist = is_playlist
        self.playlist_end = playlist_end
        self.seen_items = 0
        self.downloaded_count = 0
        self.playlist_finished = False
        self._is_running = True

    def stop(self):
        self._is_running = False

    def run(self):
        cmd = [get_ytdlp_executable(), "--no-warnings", "-P", self.output_path]
        if FFMPEG_LOCATION:
            cmd.extend(["--ffmpeg-location", FFMPEG_LOCATION])
        if self.cookie_args:
            cmd.extend(self.cookie_args)

        error_occurred = False
        error_message = ""
        try:
            # Format Selection Logic
            if self.filetype.startswith("Audio Only"):
                cmd.append("-x")  # Extract audio
                if "MP3" in self.filetype:
                    cmd.extend(["--audio-format", "mp3", "--audio-quality", "0"])
                elif "M4A" in self.filetype:
                    cmd.extend(["--audio-format", "m4a", "--audio-quality", "0"])
                elif "OPUS" in self.filetype:
                    cmd.extend(["--audio-format", "opus", "--audio-quality", "0"])
                elif "WAV" in self.filetype:
                    cmd.extend(["--audio-format", "wav"])
                else:
                    cmd.append("-f bestaudio/best")
            elif self.quality == "Best Available":
                cmd.append("-f bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best")
            elif self.quality.endswith("p"):
                try:
                    height = int(self.quality[:-1])
                    format_filter = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
                    cmd.extend(["-f", format_filter])
                    if self.filetype != "Best Available" and not self.filetype.startswith("Audio Only"):
                        clean_ext = self.filetype.split()[0].lower()
                        cmd.extend(["--merge-output-format", clean_ext])
                except ValueError:
                    self.output_signal.output_received.emit(f"Warning: Invalid quality '{self.quality}'. Fallback to best.\n")
                    cmd.append("-f bestvideo+bestaudio/best")
            else:
                self.output_signal.output_received.emit(f"Warning: Unknown quality '{self.quality}'. Fallback to best.\n")
                cmd.append("-f bestvideo+bestaudio/best")

            if self.is_playlist:
                cmd.extend([
                    "--yes-playlist",
                    "--ignore-errors",
                    "--no-abort-on-error",
                    "-o", "%(playlist_title)s/%(playlist_index)02d - %(title)s.%(ext)s"
                ])
                if self.playlist_end:
                    cmd.extend(["--playlist-end", str(self.playlist_end)])
            else:
                cmd.append("--no-playlist")

            cmd.append(self.url)
            self.output_signal.output_received.emit(f"Executing: {' '.join(cmd)}\n")

            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding='utf-8', errors='replace', bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )

            with process.stdout:
                for line in iter(process.stdout.readline, ''):
                    if not self._is_running:
                        process.terminate()
                        self.output_signal.output_received.emit("Termination signal sent.\n")
                        break
                    self.output_signal.output_received.emit(line)
                    self._parse_progress(line)
            process.wait()

            if not self._is_running:
                error_occurred = True
                error_message = "Download cancelled by user."
            elif self.is_playlist and process.returncode in (1, 101):
                # When downloading playlists/mixes with --ignore-errors, exit code 1 or 101
                # is returned by yt-dlp if any individual track was unavailable/private.
                # If tracks were downloaded or scanned, treat this as a completed playlist with skipped tracks!
                if self.playlist_finished or self.seen_items > 0 or self.downloaded_count > 0:
                    self.output_signal.output_received.emit("\n✅ Playlist download finished! (Unavailable/blocked tracks were skipped).\n")
                    self.output_signal.download_finished.emit(True, "Playlist downloaded (skipped unavailable tracks).")
                    return
                else:
                    error_occurred = True
                    error_message = f"Download failed (exit code: {process.returncode}). Check activity log."
            elif process.returncode != 0:
                error_occurred = True
                error_message = f"Download failed (exit code: {process.returncode}). Check activity log."
            else:
                self.output_signal.output_received.emit("Download completed successfully.\n")
        except FileNotFoundError:
            error_message = "Error: yt-dlp command not found in system PATH."
            error_occurred = True
        except Exception as e:
            error_message = f"Error during download: {e}"
            error_occurred = True

        self.output_signal.download_finished.emit(not error_occurred, error_message if error_occurred else "Download finished.")

    def _parse_progress(self, line: str):
        match_item = re.search(r"\[download\]\s+Downloading\s+item\s+(\d+)\s+of\s+(\d+)", line)
        if match_item:
            cur = int(match_item.group(1))
            tot = int(match_item.group(2))
            self.seen_items = cur
            self.output_signal.stats_updated.emit("", f"Item {cur}/{tot}", "")
            self.output_signal.output_received.emit(f"\n>>> Downloading Item {cur} of {tot} ...\n")

        if "Finished downloading playlist" in line:
            self.playlist_finished = True
        elif "[download] 100%" in line or "100% of" in line:
            self.downloaded_count += 1

        match_pct = re.search(r"\[download\]\s+(\d{1,3}(?:\\.\d+)?)%", line)
        if match_pct:
            try:
                percent = int(float(match_pct.group(1)))
                self.output_signal.progress_updated.emit(percent)
            except ValueError:
                pass

        match_speed = re.search(r"at\s+([\d\.]+\s*\w+/s)", line)
        match_eta = re.search(r"ETA\s+([\d:]+)", line)
        match_size = re.search(r"of\s+~?\s*([\d\.]+\s*\w+)", line)
        speed = match_speed.group(1) if match_speed else ""
        eta = match_eta.group(1) if match_eta else ""
        size = match_size.group(1) if match_size else ""
        if speed or eta or size:
            self.output_signal.stats_updated.emit(speed, eta, size)


class MetadataFetcherThread(QThread):
    """Fetches video metadata using yt-dlp -J in a separate thread."""
    def __init__(self, url: str, metadata_signal: MetadataSignal, cookie_args=None):
        super().__init__()
        self.url = url
        self.metadata_signal = metadata_signal
        self.cookie_args = cookie_args or []

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
            if FFMPEG_LOCATION:
                cmd.extend(["--ffmpeg-location", FFMPEG_LOCATION])
            if self.cookie_args:
                cmd.extend(self.cookie_args)
            cmd.append(self.url)

            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )

            stdout = result.stdout.strip()
            if not stdout:
                err = result.stderr.strip() if result.stderr else f"yt-dlp exited with code {result.returncode}"
                err_line = err.strip().split('\n')[-1]
                final_err = err_line.split("ERROR:", 1)[-1].strip() if "ERROR:" in err_line else f"yt-dlp error: {err_line[:160]}"
                self.metadata_signal.error_occurred.emit(final_err)
                return

            try:
                video_info = json.loads(stdout)
                self.metadata_signal.metadata_ready.emit(video_info)
            except Exception as e:
                self.metadata_signal.error_occurred.emit(f"Error parsing metadata: {e}")
        except FileNotFoundError:
            self.metadata_signal.error_occurred.emit("yt-dlp executable not found in PATH.")
        except Exception as e:
            self.metadata_signal.error_occurred.emit(f"Metadata fetch error: {e}")


class EngineUpdateThread(QThread):
    """Executes yt-dlp -U to upgrade the engine to the latest official release."""
    output_received = pyqtSignal(str)
    update_finished = pyqtSignal(bool, str)

    def run(self):
        try:
            self.output_received.emit("\n--- Checking for yt-dlp Engine Updates ---\n")
            process = subprocess.Popen(
                [get_ytdlp_executable(), "-U"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding='utf-8', errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            with process.stdout:
                for line in iter(process.stdout.readline, ''):
                    self.output_received.emit(line)
            process.wait()
            success = process.returncode == 0
            msg = "yt-dlp engine is up to date!" if success else f"Update exited with code {process.returncode}"
            self.update_finished.emit(success, msg)
        except Exception as e:
            self.output_received.emit(f"Update error: {e}\n")
            self.update_finished.emit(False, str(e))


def ensure_arrow_icons():
    """Generates crisp modern antialiased vector chevron icons in the app directory."""
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()

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

        # Sensible default download directory: user Downloads folder (check D: and C:)
        candidates = [
            os.path.join(os.path.expanduser("~"), "Downloads"),
            os.path.join(os.path.expanduser("~"), "Desktop"),
            r"D:\angel\Downloads",
            r"D:\angel\Desktop"
        ]
        self.output_dir = next((p for p in candidates if os.path.isdir(p)), os.path.expanduser("~"))

        self.video_info = {}
        self.download_thread = None
        self.metadata_thread = None
        self.thumbnail_thread = None
        self.quality_data = {}
        self.is_log_expanded = True

        # Signals
        self.output_signal = OutputSignal()
        self.output_signal.output_received.connect(self.update_log)
        self.output_signal.progress_updated.connect(self.update_progress)
        self.output_signal.stats_updated.connect(self.update_stats)
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
        self.check_ytdlp_exists()

    def center_window(self):
        """Centers the window on the active desktop screen."""
        qr = self.frameGeometry()
        cp = QDesktopWidget().availableGeometry().center()
        qr.moveCenter(cp)
        self.move(qr.topLeft())

    def check_ytdlp_exists(self):
        """Checks if yt-dlp is accessible; if missing, displays setup helper dialog."""
        ytdlp_bin = get_ytdlp_executable()
        is_available = False
        try:
            res = subprocess.run(
                [ytdlp_bin, "--version"], capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            is_available = (res.returncode == 0)
        except Exception:
            is_available = False

        if not is_available:
            self.set_status_pill("● Setup Required", "#F59E0B", "#2A2318")
            self.status_label.setText("Engine Setup Required")
            self.update_log(
                "===========================================================\n"
                "  Notice: 'yt-dlp' engine is not yet installed on this PC.\n"
                "  Quick Setup (10 Seconds):\n"
                "  1. Open Command Prompt / PowerShell and run:\n"
                "         winget install yt-dlp\n"
                "  2. Or download yt-dlp.exe from GitHub:\n"
                "         https://github.com/yt-dlp/yt-dlp/releases\n"
                "     and drop it into this folder!\n"
                "===========================================================\n"
            )
            # Show helpful setup dialog without crashing
            QTimer.singleShot(400, self.show_engine_setup_dialog)
        else:
            self.set_status_pill("● Ready", "#10B981", "#1A2234")
            self.status_label.setText("Ready")

    def show_engine_setup_dialog(self):
        """Shows a helpful setup dialog with WinGet copy and GitHub download buttons."""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("OmniStream Studio - Setup Required")
        msg_box.setIcon(QMessageBox.Information)
        msg_box.setText("<h3>Core Engine (yt-dlp) Required</h3>")
        msg_box.setInformativeText(
            "OmniStream Studio uses the open-source <b>yt-dlp</b> engine to download videos and audio.<br><br>"
            "<b>Option 1 (Fastest & Automatic - 10 Seconds):</b><br>"
            "Open Command Prompt or PowerShell and type:<br>"
            "<span style='color: #00DFD8; font-weight: bold;'>winget install yt-dlp</span><br><br>"
            "<b>Option 2 (Manual from GitHub):</b><br>"
            "Download <b>yt-dlp.exe</b> from GitHub and place it in the same folder as this application."
        )
        copy_btn = msg_box.addButton("📋 Copy WinGet Command", QMessageBox.ActionRole)
        github_btn = msg_box.addButton("🌐 Open GitHub Release", QMessageBox.ActionRole)
        close_btn = msg_box.addButton("OK", QMessageBox.RejectRole)

        msg_box.exec_()
        if msg_box.clickedButton() == copy_btn:
            clipboard = QApplication.clipboard()
            clipboard.setText("winget install yt-dlp")
            self.update_log("Copied to clipboard: 'winget install yt-dlp'. Paste this into Terminal or PowerShell!\n")
        elif msg_box.clickedButton() == github_btn:
            QDesktopServices.openUrl(QUrl("https://github.com/yt-dlp/yt-dlp/releases/latest"))

    def init_ui(self):
        """Sets up the UI with a modern QScrollArea for easy vertical resizing on any monitor."""
        self.central_widget = QWidget()
        self.central_widget.setObjectName("centralWidget")
        self.setCentralWidget(self.central_widget)

        # Root layout for central widget
        root_layout = QVBoxLayout(self.central_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Scroll Area for fluid resizing without squishing cards
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("mainScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # Inner Content Container with spacious, comfortable breathing room
        self.content_widget = QWidget()
        self.content_widget.setObjectName("scrollContent")
        content_layout = QVBoxLayout(self.content_widget)
        content_layout.setSpacing(12)
        content_layout.setContentsMargins(22, 16, 22, 20)

        # ----------------- 1. APP HEADER -----------------
        header_layout = QHBoxLayout()
        header_layout.setSpacing(10)

        # Logo Icon Badge
        logo_badge = QLabel("⚡")
        logo_badge.setObjectName("logoBadge")
        logo_badge.setFixedSize(36, 36)
        logo_badge.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(logo_badge)

        # Titles
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        app_title = QLabel("OmniStream Studio")
        app_title.setObjectName("appTitleLabel")
        app_subtitle = QLabel("Created by Angelo Golloberda • Universal Media & Stream Extractor")
        app_subtitle.setObjectName("appSubtitleLabel")
        title_box.addWidget(app_title)
        title_box.addWidget(app_subtitle)
        header_layout.addLayout(title_box, 1)

        # Live Status Pill
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

        url_header = QLabel("MEDIA / STREAM URL")
        url_header.setObjectName("sectionHeaderLabel")
        url_card_layout.addWidget(url_header)

        url_input_layout = QHBoxLayout()
        url_input_layout.setSpacing(8)
        self.url_input = QLineEdit()
        self.url_input.setObjectName("urlInput")
        self.url_input.setPlaceholderText("Paste link from Webex, YouTube, Panopto, Vimeo, Twitch, etc...")
        self.url_input.setMinimumHeight(36)

        self.metadata_fetch_timer = QTimer(self)
        self.metadata_fetch_timer.setSingleShot(True)
        self.metadata_fetch_timer.timeout.connect(self.fetch_metadata)
        self.url_input.textChanged.connect(self.schedule_metadata_fetch)
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
        cookie_card = QFrame()
        cookie_card.setObjectName("surfaceCard")
        cookie_layout = QVBoxLayout(cookie_card)
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

        # Source Dropdown
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

        # File path & browse (for cookies.txt)
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

        # Helper hint
        self.cookie_hint_label = QLabel("💡 Log in to Webex or your university portal in your browser, then select that browser above to download.")
        self.cookie_hint_label.setObjectName("hintLabel")
        cookie_layout.addWidget(self.cookie_hint_label)

        content_layout.addWidget(cookie_card)

        # ----------------- 4. RICH MEDIA PREVIEW CARD -----------------
        self.media_card = QFrame()
        self.media_card.setObjectName("surfaceCard")
        media_card_layout = QHBoxLayout(self.media_card)
        media_card_layout.setContentsMargins(12, 10, 12, 10)
        media_card_layout.setSpacing(14)

        # Compact 16:9 Thumbnail (180 x 101)
        self.thumbnail_label = QLabel("▶")
        self.thumbnail_label.setObjectName("thumbnailContainer")
        self.thumbnail_label.setFixedSize(200, 112)
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        media_card_layout.addWidget(self.thumbnail_label)

        # Video Information Block
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

        # Metadata Badges Row (Duration, Views, Quality tag)
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

        # ----------------- 5. FORMAT & QUALITY SELECTION -----------------
        settings_card = QFrame()
        settings_card.setObjectName("surfaceCard")
        settings_layout = QGridLayout(settings_card)
        settings_layout.setContentsMargins(12, 9, 12, 9)
        settings_layout.setHorizontalSpacing(14)
        settings_layout.setVerticalSpacing(6)

        quality_header = QLabel("RESOLUTION / VIDEO QUALITY")
        quality_header.setObjectName("sectionHeaderLabel")
        format_header = QLabel("MEDIA CONTAINER / AUDIO FORMAT")
        format_header.setObjectName("sectionHeaderLabel")
        settings_layout.addWidget(quality_header, 0, 0)
        settings_layout.addWidget(format_header, 0, 1)

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
        settings_layout.addWidget(self.filetype_combo, 1, 1)

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
        settings_layout.addWidget(self.playlist_scope_combo, 2, 1)

        content_layout.addWidget(settings_card)

        # ----------------- 6. DESTINATION FOLDER CARD -----------------
        folder_card = QFrame()
        folder_card.setObjectName("surfaceCard")
        folder_card_layout = QHBoxLayout(folder_card)
        folder_card_layout.setContentsMargins(12, 8, 12, 8)
        folder_card_layout.setSpacing(8)

        folder_icon = QLabel("📁")
        folder_icon.setObjectName("iconLabel")
        folder_card_layout.addWidget(folder_icon)

        folder_label_title = QLabel("Save To:")
        folder_label_title.setObjectName("boldLabel")
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

        self.open_folder_button = QPushButton("Reveal")
        self.open_folder_button.setObjectName("ghostButton")
        self.open_folder_button.setCursor(QCursor(Qt.PointingHandCursor))
        self.open_folder_button.setToolTip("Open folder in Windows Explorer")
        self.open_folder_button.clicked.connect(self.open_output_folder)
        folder_card_layout.addWidget(self.open_folder_button)

        content_layout.addWidget(folder_card)

        # ----------------- 7. DOWNLOAD ACTION & PROGRESS HUD -----------------
        action_card = QFrame()
        action_card.setObjectName("surfaceCard")
        action_layout = QVBoxLayout(action_card)
        action_layout.setContentsMargins(12, 10, 12, 10)
        action_layout.setSpacing(8)

        # Primary Download Button
        self.download_button = QPushButton("⬇  Start Download")
        self.download_button.setObjectName("downloadButton")
        self.download_button.setCursor(QCursor(Qt.PointingHandCursor))
        self.download_button.setMinimumHeight(42)
        self.download_button.clicked.connect(self.handle_download_button_click)
        self.download_button.setEnabled(False)
        action_layout.addWidget(self.download_button)

        # Sleek Progress Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("downloadProgressBar")
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(10)
        action_layout.addWidget(self.progress_bar)

        # Live HUD Status Row (Speed, Size, ETA, Percentage)
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

        self.toggle_log_btn = QPushButton("▾ Collapse")
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

        # Place content into scroll area
        self.scroll_area.setWidget(self.content_widget)
        root_layout.addWidget(self.scroll_area)

        # Initial reset
        self.reset_metadata_ui()

    def toggle_console(self):
        """Toggles visibility of the activity log console to save vertical space."""
        self.is_log_expanded = not self.is_log_expanded
        self.log_output.setVisible(self.is_log_expanded)
        self.toggle_log_btn.setText("▾ Collapse" if self.is_log_expanded else "▸ Expand")

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
                font-size: 9.5pt;
                font-weight: 600;
            }
            QLabel#iconLabel {
                font-size: 11pt;
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
                padding: 6px 10px;
                font-size: 9.5pt;
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
                padding: 6px 12px;
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
                line-height: 1.25;
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
                image: url({normal_arrow});
                width: 12px;
                height: 12px;
            }
            QComboBox#customComboBox::down-arrow:hover {
                image: url({hover_arrow});
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
    def on_cookie_source_changed(self, index=None):
        """Updates cookie controls and helper hint based on selected source."""
        source = self.cookie_source_combo.currentText()
        is_custom_file = "Custom File" in source
        self.cookie_file_input.setEnabled(is_custom_file)
        self.cookie_browse_button.setEnabled(is_custom_file)

        if "Chrome" in source:
            self.cookie_hint_label.setText("💡 Logged into Webex in Google Chrome? Session cookies will be extracted automatically.")
            self.update_log("Auth: Using Chrome session cookies.\n")
        elif "Edge" in source:
            self.cookie_hint_label.setText("💡 Logged into Webex in Microsoft Edge? Session cookies will be extracted automatically.")
            self.update_log("Auth: Using Edge session cookies.\n")
        elif "Firefox" in source:
            self.cookie_hint_label.setText("💡 Logged into Webex in Mozilla Firefox? Session cookies will be extracted automatically.")
            self.update_log("Auth: Using Firefox session cookies.\n")
        elif "Brave" in source:
            self.cookie_hint_label.setText("💡 Logged into Webex in Brave? Session cookies will be extracted automatically.")
            self.update_log("Auth: Using Brave session cookies.\n")
        elif is_custom_file:
            self.cookie_hint_label.setText("💡 Export cookies from your browser using an extension and select the file above.")
            self.update_log("Auth: Using custom cookies.txt file.\n")
        else:
            self.cookie_hint_label.setText("💡 Log in to your university portal in your browser, then select that browser above to download.")
            self.update_log("Auth: No cookies specified (public access).\n")

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
        """Clears URL input and resets all UI states."""
        self.metadata_fetch_timer.stop()
        self.url_input.clear()
        self.reset_metadata_ui()
        self.set_status_pill("● Ready", "#10B981", "#1A2234")

    def choose_output_folder(self):
        """Allows user to pick target folder via file dialog."""
        start_dir = self.output_dir or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Select Download Directory", start_dir)
        if folder:
            self.output_dir = folder
            self.output_dir_label.setText(folder)
            self.output_dir_label.setToolTip(folder)
            self.update_log(f"Target folder set to: {folder}\n")

    def open_output_folder(self):
        """Opens target directory directly in Windows Explorer."""
        if self.output_dir and os.path.isdir(self.output_dir):
            try:
                os.startfile(self.output_dir)
            except Exception as e:
                QDesktopServices.openUrl(QUrl.fromLocalFile(self.output_dir))
        else:
            QMessageBox.warning(self, "Folder Not Found", "The selected destination folder does not exist.")

    def schedule_metadata_fetch(self):
        """Debounced fetch trigger when user types in URL input."""
        text = self.url_input.text().strip()
        if not text:
            self.reset_metadata_ui()
            self.set_status_pill("● Ready", "#10B981", "#1A2234")
            return
        self.set_status_pill("● Checking...", "#00DFD8", "#1A2738")
        self.metadata_fetch_timer.start(500)

    def _is_valid_url(self, url: str) -> bool:
        return bool(url) and not url.isspace() and ("http://" in url or "https://" in url or "webex" in url or "youtu" in url)

    # --- Metadata Handling ---
    def fetch_metadata(self):
        """Starts fetching video metadata on background thread."""
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
            self.show_engine_setup_dialog()
            return

        if self.metadata_thread and self.metadata_thread.isRunning():
            self.metadata_thread.wait(200)
        if self.thumbnail_thread and self.thumbnail_thread.isRunning():
            self.thumbnail_thread.wait(200)

        self.reset_metadata_ui()
        self.set_ui_loading_state(True, "Loading metadata...")
        self.set_status_pill("● Fetching Info", "#00DFD8", "#1A2738")
        self.update_log(f"Fetching stream details: {url} ...\n")

        cookie_args = self.get_cookie_args()
        self.metadata_thread = MetadataFetcherThread(url, self.metadata_signal, cookie_args=cookie_args)
        self.metadata_thread.start()

    def on_playlist_scope_changed(self, index):
        """Updates download button text based on playlist vs single video scope."""
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
            elif user_data == "all":
                self.download_button.setText(f"⚡ Download Complete Mix ({count_str} Videos)")
            else:
                self.download_button.setText(f"⚡ Download First {user_data} Videos")

    def on_metadata_ready(self, video_info: dict):
        """Populates UI cards with fetched metadata."""
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
            self.update_log(f"Loaded Playlist/Mix: {clean_title} ({count_str} items found)\n")

            self.channel_badge.setText(f"📁 Playlist ({count_str} items)")
            self.duration_badge.setText("⏱ Multiple Tracks")
            self.views_badge.setText(f"🔢 {count_str} Videos")

            self.status_label.setText("Playlist Ready")
            self.set_status_pill("● Playlist Ready", "#10B981", "#1A2234")

            # Setup Playlist Scope selector with accurate item subsets
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

            # Playlist thumbnail
            thumbnail_url = self.video_info.get('thumbnail')
            if not thumbnail_url and entries:
                first_entry = entries[0]
                thumbnail_url = first_entry.get('thumbnail')
                if not thumbnail_url and first_entry.get('id'):
                    thumbnail_url = f"https://i.ytimg.com/vi/{first_entry.get('id')}/hqdefault.jpg"

            if thumbnail_url:
                if self.thumbnail_thread and self.thumbnail_thread.isRunning():
                    self.thumbnail_thread.wait(200)
                self.thumbnail_thread = ThumbnailLoaderThread(thumbnail_url, self.thumbnail_signal)
                self.thumbnail_thread.start()

            # Quality Options for Playlist
            self.quality_data.clear()
            self.quality_combo.clear()
            self.quality_combo.addItem("Best Available (Highest Resolution)")
            self.quality_data["Best Available (Highest Resolution)"] = "Best Available"
            for q in ["1080p", "720p", "480p", "360p"]:
                display_q = f"{q} (If Available)"
                self.quality_combo.addItem(display_q)
                self.quality_data[display_q] = q

            self.quality_combo.setEnabled(True)

            # Formats for Playlist
            self.filetype_combo.clear()
            self.filetype_combo.addItem("Best Available")
            self.filetype_combo.addItem("MP4 (Universal Video)")
            self.filetype_combo.addItem("MKV (High Definition)")
            self.filetype_combo.insertSeparator(self.filetype_combo.count())
            self.filetype_combo.addItem("Audio Only (MP3 - High Quality)")
            self.filetype_combo.addItem("Audio Only (M4A - Apple AAC)")
            self.filetype_combo.addItem("Audio Only (OPUS)")
            self.filetype_combo.setEnabled(True)

            self.download_button.setText(f"⚡ Download Complete Mix ({count} Videos)")
            self.download_button.setEnabled(True)
            self.set_ui_loading_state(False)
            return

        # Single video handling
        self.playlist_scope_label.setVisible(False)
        self.playlist_scope_combo.setVisible(False)
        self.download_button.setText("⚡ Start Download")

        title = self.video_info.get("title", "Unknown Stream / Recording")
        self.video_title_label.setText(title)
        self.update_log(f"Loaded: {title}\n")

        # Channel & Badges
        channel = self.video_info.get("uploader") or self.video_info.get("channel") or "Media Stream"
        self.channel_badge.setText(f"👤 {channel}")

        duration_sec = self.video_info.get("duration")
        self.duration_badge.setText(f"⏱ {format_duration(duration_sec)}")

        views_count = self.video_info.get("view_count")
        self.views_badge.setText(f"👁 {format_views(views_count)}" if views_count else "🔒 Private/Stream")

        self.status_label.setText("Metadata Ready")
        self.set_status_pill("● Ready to Download", "#10B981", "#1A2234")

        # Trigger background thumbnail fetch
        thumbnail_url = self.video_info.get('thumbnail')
        if thumbnail_url:
            if self.thumbnail_thread and self.thumbnail_thread.isRunning():
                self.thumbnail_thread.wait(200)
            self.thumbnail_thread = ThumbnailLoaderThread(thumbnail_url, self.thumbnail_signal)
            self.thumbnail_thread.start()

        # Populate Quality ComboBox
        formats = self.video_info.get("formats", [])
        self.quality_data.clear()
        self.quality_combo.clear()
        self.quality_combo.addItem("Best Available (Highest Resolution)")
        self.quality_data["Best Available (Highest Resolution)"] = "Best Available"

        if not formats:
            self.quality_combo.setEnabled(True)
            self.filetype_combo.clear()
            self.filetype_combo.addItem("Best Available")
            self.filetype_combo.setEnabled(True)
            self.download_button.setEnabled(True)
            self.set_ui_loading_state(False)
            self.update_log("Notice: Best Available format stream will be used.\n")
            return

        # Find best audio size
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

        unique_heights = sorted(list(set(f['height'] for f in formats if f.get('height') and f.get('vcodec') != 'none')), reverse=True)
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

        # Populate Media Format ComboBox
        self.filetype_combo.clear()
        self.filetype_combo.addItem("Best Available")
        self.filetype_combo.addItem("MP4 (Universal Video)")
        self.filetype_combo.addItem("MKV (High Definition)")
        self.filetype_combo.addItem("WebM (Modern Web)")
        self.filetype_combo.insertSeparator(self.filetype_combo.count())
        self.filetype_combo.addItem("Audio Only (MP3 - High Quality)")
        self.filetype_combo.addItem("Audio Only (M4A - Apple AAC)")
        self.filetype_combo.addItem("Audio Only (OPUS)")
        self.filetype_combo.addItem("Audio Only (WAV - Lossless)")
        self.filetype_combo.setEnabled(True)

        self.download_button.setEnabled(True)
        self.set_ui_loading_state(False)

    def on_thumbnail_ready(self, img_data: bytes):
        """Renders fetched thumbnail with rounded antialiased corners."""
        try:
            image = QImage()
            if image.loadFromData(img_data):
                pixmap = QPixmap.fromImage(image)
                target_w = self.thumbnail_label.width()
                target_h = self.thumbnail_label.height()
                scaled = pixmap.scaled(target_w, target_h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)

                # Crop center to 16:9
                x = (scaled.width() - target_w) // 2
                y = (scaled.height() - target_h) // 2
                cropped = scaled.copy(x, y, target_w, target_h)

                # Round corners with QPainter
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

    def on_thumbnail_error(self, err_msg: str):
        self.thumbnail_label.setText("🎬")

    def on_metadata_error(self, error_message: str):
        """Handles errors during metadata fetch."""
        self.update_log(f"Metadata Error: {error_message}\n")
        if "login" in error_message.lower() or "authentication" in error_message.lower() or "private" in error_message.lower():
            self.status_label.setText("Login/Cookies Required")
            self.update_log("Tip: Select your browser in 'Authentication & Cookies' above where you are logged into Webex.\n")
        else:
            self.status_label.setText("Error loading metadata")

        self.set_status_pill("● Fetch Error", "#EF4444", "#2A1818")
        self.reset_metadata_ui()
        self.set_ui_loading_state(False)
        if "not found in PATH" in error_message:
            QMessageBox.critical(self, "yt-dlp Missing", error_message)

    def reset_metadata_ui(self):
        """Resets video cards to clean default state."""
        self.video_info = {}
        self.quality_data.clear()
        self.video_title_label.setText("Paste a video link above")
        self.channel_badge.setText("Waiting for URL input...")
        self.duration_badge.setText("--:--")
        self.views_badge.setText("0 views")
        self.status_label.setText("Ready")

        self.thumbnail_label.setPixmap(QPixmap())
        self.thumbnail_label.setText("▶")

        self.quality_combo.clear()
        self.quality_combo.addItem("Waiting for video...")
        self.quality_combo.setEnabled(False)

        self.filetype_combo.clear()
        self.filetype_combo.addItem("Waiting for video...")
        self.filetype_combo.setEnabled(False)

        self.download_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.hud_speed_size.setText("⚡ Ready to download")
        self.hud_eta_percent.setText("0%")

    def set_ui_loading_state(self, loading: bool, status_text: str = ""):
        """Controls enabling of interactive controls during fetch."""
        is_ready = not loading and bool(self.video_info)
        self.url_input.setEnabled(not loading)
        self.paste_button.setEnabled(not loading)
        self.clear_button.setEnabled(not loading)
        self.choose_dir_button.setEnabled(not loading)
        self.open_folder_button.setEnabled(not loading)
        self.cookie_source_combo.setEnabled(not loading)
        self.cookie_browse_button.setEnabled(not loading and ("Custom File" in self.cookie_source_combo.currentText()))
        self.quality_combo.setEnabled(is_ready)
        self.filetype_combo.setEnabled(is_ready)
        self.download_button.setEnabled(is_ready)
        if loading and status_text:
            self.status_label.setText(status_text)

    def set_status_pill(self, text: str, fg: str, bg: str):
        """Updates the header status badge."""
        self.status_pill.setText(text)
        self.status_pill.setStyleSheet(f"background-color: {bg}; color: {fg}; font-size: 8pt; font-weight: 600; padding: 4px 10px; border-radius: 10px; border: 1px solid {fg}40;")

    # --- Download Handling ---
    def handle_download_button_click(self):
        """Handles click on Start / Cancel download button."""
        if self.download_thread and self.download_thread.isRunning():
            self.download_thread.stop()
            self.set_status_pill("● Cancelling...", "#F59E0B", "#2A2318")
            self.hud_speed_size.setText("Cancelling download...")
            return
        self.start_download()

    def start_download(self):
        """Validates inputs and launches DownloadThread."""
        if not self.output_dir or not os.path.isdir(self.output_dir):
            QMessageBox.warning(self, "Output Directory Required", "Please select a valid destination folder using 'Browse...'.")
            return

        url = self.url_input.text().strip()
        if not self._is_valid_url(url) or not self.video_info:
            QMessageBox.warning(self, "Metadata Required", "Please load a valid video URL before downloading.")
            return

        quality_display = self.quality_combo.currentText()
        quality = self.quality_data.get(quality_display, "Best Available")
        filetype = self.filetype_combo.currentText()
        cookie_args = self.get_cookie_args()

        self.set_ui_downloading_state(True)
        self.progress_bar.setValue(0)
        self.set_status_pill("● Downloading", "#FF0055", "#2A1820")
        self.hud_speed_size.setText("⚡ Initializing stream...")
        self.hud_eta_percent.setText("0%")

        cookie_display = ' '.join(cookie_args) if cookie_args else 'None'
        self.update_log(
            f"--- Starting Download ---\n"
            f"Title: {self.video_info.get('title')}\n"
            f"Quality: {quality}\n"
            f"Format: {filetype}\n"
            f"Folder: {self.output_dir}\n"
            f"Cookies: {cookie_display}\n"
        )

        is_playlist = False
        playlist_end = None
        if hasattr(self, 'playlist_scope_combo') and self.playlist_scope_combo.isVisible():
            scope_mode = self.playlist_scope_combo.currentData()
            if scope_mode != "single":
                is_playlist = True
                if isinstance(scope_mode, int):
                    playlist_end = scope_mode

        self.download_thread = DownloadThread(
            url,
            quality,
            filetype,
            self.output_signal,
            self.output_dir,
            cookie_args=cookie_args,
            is_playlist=is_playlist,
            playlist_end=playlist_end
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
        self.cookie_source_combo.setEnabled(enable)
        self.cookie_browse_button.setEnabled(enable and ("Custom File" in self.cookie_source_combo.currentText()))
        self.quality_combo.setEnabled(enable and bool(self.video_info))
        self.filetype_combo.setEnabled(enable and bool(self.video_info))
        if hasattr(self, 'playlist_scope_combo'):
            self.playlist_scope_combo.setEnabled(enable)

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
            self.download_button.setText("⬇  Start Download")
            self.download_button.setObjectName("downloadButton")
            self.download_button.setStyleSheet("")  # Reverts to base stylesheet

    def on_download_complete(self, success: bool, message: str):
        """Handles post-download UI updates."""
        self.set_ui_downloading_state(False)
        self.update_log(f"Result: {message}\n")

        if success:
            self.progress_bar.setValue(100)
            if "skipped" in message.lower():
                self.set_status_pill("● Completed (with skips)", "#10B981", "#1A2234")
                self.hud_speed_size.setText(f"✅ {message} Saved to target folder.")
            else:
                self.set_status_pill("● Completed!", "#10B981", "#1A2234")
                self.hud_speed_size.setText("✅ Download complete! Saved to target folder.")
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
        """Updates progress bar and percentage HUD."""
        self.progress_bar.setValue(percent)
        self.hud_eta_percent.setText(f"{percent}%")

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
        self.metadata_fetch_timer.stop()
        if self.metadata_thread and self.metadata_thread.isRunning():
            self.metadata_thread.wait(500)
        if self.thumbnail_thread and self.thumbnail_thread.isRunning():
            self.thumbnail_thread.wait(500)

        if self.download_thread and self.download_thread.isRunning():
            reply = QMessageBox.question(
                self, 'Confirm Exit',
                "A download is currently in progress. Stop and exit?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.download_thread.stop()
                self.download_thread.wait(1000)
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
