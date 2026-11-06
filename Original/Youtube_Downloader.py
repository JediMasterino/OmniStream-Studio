# -*- coding: utf-8 -*-
import sys
import subprocess
import threading
import json
import re
import os # <-- Needed for os.path.isdir
import ssl
from urllib.request import urlopen, URLError
from urllib.error import HTTPError, URLError
from socket import timeout as TimeoutError
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLineEdit, QPushButton, QTextEdit, QLabel, QComboBox, QSizePolicy,
    QFileDialog, QProgressBar, QMessageBox, QDesktopWidget, QAbstractItemView
)
from PyQt5.QtCore import pyqtSignal, QObject, Qt, QThread, QTimer
from PyQt5.QtGui import QPixmap, QFont, QPalette, QColor, QImage

# --- Constants ---
MIN_WINDOW_WIDTH = 600
MIN_WINDOW_HEIGHT = 700
APP_NAME = "Youtube Downloader" # <-- App Name

# --- Helper Function ---
def format_bytes(size_bytes):
    """Converts bytes to a human-readable string (KB, MB, GB)."""
    if size_bytes is None:
        return "N/A"
    if size_bytes == 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while size_bytes >= 1024 and i < len(size_name) - 1:
        size_bytes /= 1024.0
        i += 1
    s = f"{size_bytes:.1f}" if i > 0 else f"{int(size_bytes)}"
    return f"{s} {size_name[i]}"

# --- Signals ---
class OutputSignal(QObject):
    output_received = pyqtSignal(str)
    progress_updated = pyqtSignal(int)
    download_finished = pyqtSignal(bool, str)

class MetadataSignal(QObject):
    metadata_ready = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)

# --- Threads ---
class DownloadThread(QThread):
    """Handles the yt-dlp download process in a separate thread."""
    def __init__(self, url: str, quality: str, filetype: str, output_signal: OutputSignal, output_path: str):
        super().__init__()
        self.url = url
        self.quality = quality
        self.filetype = filetype
        self.output_signal = output_signal
        self.output_path = output_path
        self._is_running = True

    def stop(self):
        self._is_running = False

    def run(self):
        cmd = ["yt-dlp", "--no-warnings", "-P", self.output_path]
        error_occurred = False
        error_message = ""
        try:
            # Format Selection Logic
            if self.filetype.startswith("Audio Only"):
                cmd.append("-x") # Extract audio
                if "MP3" in self.filetype: cmd.extend(["--audio-format", "mp3", "--audio-quality", "0"])
                elif "M4A" in self.filetype: cmd.extend(["--audio-format", "m4a", "--audio-quality", "0"])
                elif "OPUS" in self.filetype: cmd.extend(["--audio-format", "opus", "--audio-quality", "0"])
                elif "WAV" in self.filetype: cmd.extend(["--audio-format", "wav"])
                else: cmd.append("-f bestaudio/best") # Default to best audio
            elif self.quality == "Best Available":
                 # Let yt-dlp choose the best video/audio muxed format
                 cmd.append("-f bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best")
            elif self.quality.endswith("p"):
                try:
                    height = int(self.quality[:-1])
                    format_filter = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
                    cmd.extend(["-f", format_filter])
                    # Apply container choice unless it's audio only or best available
                    if self.filetype != "Best Available" and not self.filetype.startswith("Audio Only"):
                        cmd.extend(["--merge-output-format", self.filetype.lower()])
                except ValueError:
                    self.output_signal.output_received.emit(f"Warning: Invalid quality format '{self.quality}'. Falling back to best.\n")
                    cmd.append("-f bestvideo+bestaudio/best")
            else:
                 # Fallback if quality string is unexpected
                 self.output_signal.output_received.emit(f"Warning: Unknown quality '{self.quality}'. Falling back to best.\n")
                 cmd.append("-f bestvideo+bestaudio/best")

            cmd.append(self.url)
            self.output_signal.output_received.emit(f"Executing command: {' '.join(cmd)}\n")

            # Execute yt-dlp
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding='utf-8', errors='replace', bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            # Read output line by line
            with process.stdout:
                for line in iter(process.stdout.readline, ''):
                    if not self._is_running:
                        process.terminate()
                        self.output_signal.output_received.emit("Termination signal sent.\n")
                        break
                    self.output_signal.output_received.emit(line)
                    self._parse_progress(line)
            process.wait()

            # Check result after process finishes or is stopped
            if not self._is_running:
                 if process.returncode not in [None, 0, -9, -15]: # Check common termination codes
                    error_occurred = True; error_message = f"Cancelled, exit code: {process.returncode}."
                 else: error_occurred = True; error_message = "Download cancelled."
            elif process.returncode != 0:
                error_occurred = True; error_message = f"Download failed (code: {process.returncode}). Check log."
            else:
                self.output_signal.output_received.emit("Download completed successfully.\n")
        except FileNotFoundError:
             error_message = "Error: yt-dlp command not found."; error_occurred = True
        except Exception as e:
            error_message = f"Error during download setup: {e}"; error_occurred = True
        # Emit final signal
        self.output_signal.download_finished.emit(not error_occurred, error_message if error_occurred else "Download finished.")

    def _parse_progress(self, line: str):
        match = re.search(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%", line)
        if match:
            try: self.output_signal.progress_updated.emit(int(float(match.group(1))))
            except ValueError: pass # Ignore parsing errors

class MetadataFetcherThread(QThread):
    """Fetches video metadata using yt-dlp -J in a separate thread."""
    def __init__(self, url: str, metadata_signal: MetadataSignal):
        super().__init__()
        self.url = url
        self.metadata_signal = metadata_signal
    def run(self):
        try:
            cmd = ["yt-dlp", "--no-warnings", "--skip-download", "-J", self.url]
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            try:
                video_info = json.loads(result.stdout)
                self.metadata_signal.metadata_ready.emit(video_info)
            except Exception as e:
                 self.metadata_signal.error_occurred.emit(f"Error processing metadata: {e}")
        except FileNotFoundError:
             self.metadata_signal.error_occurred.emit("Error: yt-dlp command not found.")
        except subprocess.CalledProcessError as e:
            err = e.stderr or e.stdout or f"yt-dlp exited code {e.returncode}"
            err_line = err.strip().split('\n')[-1] # Get last line for summary
            # Extract error message if yt-dlp provided one
            final_err = err_line.split("ERROR:", 1)[-1].strip() if "ERROR:" in err_line else f"yt-dlp error: {err_line[:200]}..."
            self.metadata_signal.error_occurred.emit(final_err)
        except Exception as e:
            self.metadata_signal.error_occurred.emit(f"Metadata fetch error: {e}")

# --- Main Window ---
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self.center_window()
        self.setAttribute(Qt.WA_TranslucentBackground) # For transparency attempt

        self.output_dir = None # <-- Initialize to None, require user selection
        self.video_info = {}
        self.download_thread = None
        self.metadata_thread = None
        self.quality_data = {} # Map display text (e.g., "1080p (150MB)") to actual value ("1080p")

        # Signals
        self.output_signal = OutputSignal()
        self.output_signal.output_received.connect(self.update_log)
        self.output_signal.progress_updated.connect(self.update_progress)
        self.output_signal.download_finished.connect(self.on_download_complete)
        self.metadata_signal = MetadataSignal()
        self.metadata_signal.metadata_ready.connect(self.on_metadata_ready)
        self.metadata_signal.error_occurred.connect(self.on_metadata_error)

        # Init UI
        self.init_ui()
        self.apply_styles()
        self.check_ytdlp_exists()

    def center_window(self):
        """Centers the window on the screen."""
        qr = self.frameGeometry()
        cp = QDesktopWidget().availableGeometry().center()
        qr.moveCenter(cp)
        self.move(qr.topLeft())

    def check_ytdlp_exists(self):
        """Checks if yt-dlp is accessible."""
        try:
            subprocess.run(
                ["yt-dlp", "--version"], capture_output=True, check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not execute 'yt-dlp'.\nPlease check installation and PATH.\nError: {e}")
            sys.exit(1) # Exit if yt-dlp is not available

    def init_ui(self):
        """Sets up the user interface elements."""
        self.central_widget = QWidget()
        self.central_widget.setObjectName("centralWidget")
        self.setCentralWidget(self.central_widget)

        main_layout = QVBoxLayout(self.central_widget)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.addSpacing(10) # Top spacing where title was

        # Video Info Area
        video_info_layout = QHBoxLayout()
        self.thumbnail_label = QLabel("Thumbnail...")
        self.thumbnail_label.setObjectName("thumbnail_label")
        self.thumbnail_label.setFixedSize(160, 120)
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        video_info_layout.addWidget(self.thumbnail_label)
        self.video_title_label = QLabel("Video title...")
        self.video_title_label.setObjectName("video_title_label")
        self.video_title_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.video_title_label.setWordWrap(True)
        video_info_layout.addWidget(self.video_title_label, 1)
        main_layout.addLayout(video_info_layout)
        main_layout.addSpacing(10)

        # URL Input
        url_group_layout = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setObjectName("urlInput")
        self.url_input.setPlaceholderText("Enter Video URL...")
        self.url_input.setMinimumHeight(35)
        self.metadata_fetch_timer = QTimer(self)
        self.metadata_fetch_timer.setSingleShot(True)
        self.metadata_fetch_timer.timeout.connect(self.fetch_metadata)
        self.url_input.textChanged.connect(self.schedule_metadata_fetch)
        url_group_layout.addWidget(self.url_input, 1)
        self.paste_button = QPushButton("Paste")
        self.paste_button.setToolTip("Paste URL and fetch metadata")
        self.paste_button.clicked.connect(self.paste_and_fetch)
        url_group_layout.addWidget(self.paste_button)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip("Clear URL input and results")
        self.clear_button.clicked.connect(self.clear_url_input)
        url_group_layout.addWidget(self.clear_button)
        main_layout.addLayout(url_group_layout)

        # Status Label
        self.status_label = QLabel("Enter a URL to begin.")
        self.status_label.setObjectName("statusLabel")
        main_layout.addWidget(self.status_label)
        main_layout.addSpacing(5)

        # Settings (Quality, Format)
        settings_layout = QGridLayout()
        settings_layout.setColumnStretch(1, 1)
        quality_label = QLabel("Quality:")
        self.quality_combo = QComboBox()
        self.quality_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.quality_combo.setEnabled(False)
        settings_layout.addWidget(quality_label, 0, 0, Qt.AlignRight)
        settings_layout.addWidget(self.quality_combo, 0, 1)
        format_label = QLabel("Format:")
        self.filetype_combo = QComboBox()
        self.filetype_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.filetype_combo.setEnabled(False)
        settings_layout.addWidget(format_label, 1, 0, Qt.AlignRight)
        settings_layout.addWidget(self.filetype_combo, 1, 1)
        main_layout.addLayout(settings_layout)

        # Output Directory
        output_dir_layout = QHBoxLayout()
        # Display placeholder text initially
        self.output_dir_label = QLineEdit("[No folder chosen]")
        self.output_dir_label.setObjectName("outputDirLabel")
        self.output_dir_label.setReadOnly(True)
        self.output_dir_label.setToolTip("Selected download folder")
        output_dir_layout.addWidget(QLabel("Save to:"))
        output_dir_layout.addWidget(self.output_dir_label, 1)
        self.choose_dir_button = QPushButton("Browse...")
        self.choose_dir_button.setToolTip("Choose download folder")
        self.choose_dir_button.clicked.connect(self.choose_output_folder)
        output_dir_layout.addWidget(self.choose_dir_button)
        main_layout.addLayout(output_dir_layout)
        main_layout.addSpacing(15)

        # Download Button, Progress Bar, Log Output
        self.download_button = QPushButton("Download")
        self.download_button.setObjectName("downloadButton")
        self.download_button.setMinimumHeight(40)
        self.download_button.clicked.connect(self.start_download)
        self.download_button.setEnabled(False)
        main_layout.addWidget(self.download_button)
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        main_layout.addWidget(self.progress_bar)
        self.log_output = QTextEdit()
        self.log_output.setObjectName("logOutput")
        self.log_output.setReadOnly(True)
        self.log_output.setFont(QFont("Consolas", 10) if sys.platform == 'win32' else QFont("Monospace", 10))
        self.log_output.setLineWrapMode(QTextEdit.WidgetWidth)
        main_layout.addWidget(self.log_output, 1) # Log area expands

    def apply_styles(self):
        """Applies the application stylesheet."""
        bg_color = "rgba(45, 45, 45, 220)" # Semi-transparent dark background
        control_bg = "rgba(65, 65, 65, 240)" # Controls slightly more opaque
        control_fg = "white"
        disabled_fg = "#888888"
        border_color = "#555555"
        highlight_color = "#3498db" # Blue highlight

        # --- Stylesheet ---
        # NOTE: For QComboBox::down-arrow 'image: url(arrow_down.png);' to work,
        # ensure 'arrow_down.png' exists in the same directory as the script.
        # A better approach for distribution is using Qt's Resource System (.qrc).
        self.setStyleSheet(f"""
            QMainWindow {{ background-color: transparent; }} /* Required for transparency */
            QWidget#centralWidget {{ background-color: {bg_color}; border-radius: 8px; }}

            /* General Labels */
            QLabel, QLabel#video_title_label {{
                color: {control_fg}; background-color: transparent; font-size: 11pt;
            }}
            QLabel#video_title_label {{ font-weight: bold; font-size: 14px; padding-left: 10px; }}
            QLabel#thumbnail_label {{
                border: 1px solid {border_color}; background-color: rgba(60, 60, 60, 200);
                color: #aaaaaa; border-radius: 4px;
            }}
            QLabel#statusLabel {{ color: #cccccc; font-style: italic; background-color: transparent; }}

            /* Input/Text Widgets */
            QLineEdit, QTextEdit {{
                background-color: {control_bg}; color: {control_fg}; border: 1px solid {border_color};
                border-radius: 4px; padding: 6px; font-size: 11pt;
            }}
            QTextEdit#logOutput {{ font-family: Consolas, Monospace; font-size: 10pt; }}
            QLineEdit:read-only, QLineEdit#outputDirLabel {{
                background-color: rgba(55, 55, 55, 220); color: #bbbbbb;
            }}

            /* Buttons */
            QPushButton {{
                background-color: {control_bg}; color: {control_fg}; border: 1px solid {border_color};
                border-radius: 4px; padding: 8px 14px; font-size: 11pt;
            }}
            QPushButton:hover {{ background-color: rgba(85, 85, 85, 240); border: 1px solid #777777; }}
            QPushButton:pressed {{ background-color: rgba(50, 50, 50, 240); }}
            QPushButton:disabled {{ background-color: rgba(55, 55, 55, 200); color: {disabled_fg}; border: 1px solid #444444; }}
            QPushButton#downloadButton {{ font-weight: bold; font-size: 12pt; padding: 10px 16px; }}

            /* ComboBox Styling */
            QComboBox {{
                background-color: {control_bg}; color: {control_fg}; border: 1px solid {border_color};
                border-radius: 4px; padding: 6px; padding-left: 10px; font-size: 11pt; min-height: 20px;
            }}
            QComboBox:disabled {{ background-color: rgba(55, 55, 55, 200); color: {disabled_fg}; border: 1px solid #444444; }}
            QComboBox::drop-down {{ /* Area containing the arrow */
                subcontrol-origin: padding; subcontrol-position: top right; width: 22px;
                border-left-width: 1px; border-left-color: {border_color}; border-left-style: solid;
                border-top-right-radius: 4px; border-bottom-right-radius: 4px;
                background: transparent;
            }}
            QComboBox::down-arrow {{ /* The arrow image */
                image: url(arrow_down.png); /* <-- USE IMAGE */
                width: 12px; height: 12px; /* Adjust size as needed */
                 /* Center the arrow in the drop-down area */
                 /* padding: 5px; */
            }}
            QComboBox QAbstractItemView {{ /* The dropdown list view */
                background-color: {control_bg}; color: {control_fg}; border: 1px solid {border_color};
                selection-background-color: {highlight_color}; selection-color: white;
                padding: 4px; outline: 0px; /* Remove focus outline */
            }}

            /* Progress Bar */
            QProgressBar {{
                border: 1px solid {border_color}; border-radius: 4px; text-align: center;
                color: {control_fg}; background-color: {control_bg}; font-size: 10pt;
            }}
            QProgressBar::chunk {{ background-color: {highlight_color}; border-radius: 3px; margin: 1px; }}

            /* ScrollBar (Optional styling) */
            QScrollBar:vertical {{
                border: none; background: rgba(30,30,30,200); width: 10px; margin: 0; border-radius: 5px;
            }}
            QScrollBar::handle:vertical {{ background: #666666; min-height: 25px; border-radius: 5px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; background: none; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
        """)

    # --- UI Actions ---
    def paste_and_fetch(self):
        clipboard = QApplication.clipboard()
        self.url_input.setText(clipboard.text())
        self.fetch_metadata() # Trigger fetch immediately

    def clear_url_input(self):
        self.metadata_fetch_timer.stop()
        self.url_input.clear()
        self.reset_metadata_ui()
        self.status_label.setText("Enter a URL...")
        # Reset status style if needed, e.g., self.status_label.setStyleSheet("")
        self.log_output.clear()

    def choose_output_folder(self):
        # Start browser in user's home directory if no path is set yet
        start_dir = self.output_dir or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Select Download Folder", start_dir)
        if folder: # Only update if a folder was actually selected
            self.output_dir = folder
            self.output_dir_label.setText(folder)
            self.update_log(f"Download folder set: {folder}\n")

    def schedule_metadata_fetch(self):
        self.status_label.setText("Checking URL...")
        self.status_label.setStyleSheet("font-style: italic; color: #cccccc;") # Reset style
        self.metadata_fetch_timer.start(500) # Debounce

    def _is_valid_url(self, url: str) -> bool:
        """Basic check if the string looks like a URL yt-dlp might handle."""
        return bool(url) and not url.isspace()

    # --- Metadata Handling ---
    def fetch_metadata(self):
        """Starts the metadata fetching process."""
        url = self.url_input.text().strip()
        if not self._is_valid_url(url):
            self.status_label.setText("Enter a valid URL.")
            self.status_label.setStyleSheet("color: orange;") # Use color for feedback
            self.reset_metadata_ui()
            return

        if self.metadata_thread and self.metadata_thread.isRunning():
            pass # Let previous finish or be ignored by on_metadata_ready check

        self.reset_metadata_ui()
        self.set_ui_loading_state(True, "Loading metadata...")
        self.metadata_thread = MetadataFetcherThread(url, self.metadata_signal)
        self.metadata_thread.start()

    def on_metadata_ready(self, video_info: dict):
        """Handles successfully fetched metadata and populates UI."""
        # Ensure this signal is for the *current* URL in the input
        if self.url_input.text().strip() != video_info.get('webpage_url', ''):
             self.update_log("Ignoring stale metadata for a previous URL.\n")
             return # Stale data, ignore

        self.video_info = video_info
        self.update_log("Video metadata loaded successfully.\n")
        self.status_label.setText("Metadata loaded.")
        self.status_label.setStyleSheet("color: lightgreen;") # Green success
        self.video_title_label.setText(self.video_info.get("title", "N/A"))

        # Load Thumbnail
        thumbnail_url = self.video_info.get('thumbnail')
        self.load_thumbnail(thumbnail_url) if thumbnail_url else self.thumbnail_label.setText("No Thumb")

        # --- Populate Quality ComboBox with File Sizes ---
        self.quality_data.clear() # Clear previous data
        self.quality_combo.clear()
        self.quality_combo.addItem("Best Available") # Add default option first
        self.quality_data["Best Available"] = "Best Available" # Map display to value

        formats = self.video_info.get("formats", [])
        if not formats:
            self.quality_combo.setEnabled(False) # No formats found
            self.filetype_combo.setEnabled(False)
            self.download_button.setEnabled(False)
            self.set_ui_loading_state(False)
            self.update_log("Warning: No downloadable formats found in metadata.\n")
            return

        # Find the best overall audio format size (approx)
        best_audio_size = 0; best_audio_abr = 0
        for f in formats:
            if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                 abr = f.get('abr', 0) or 0
                 fsize = f.get('filesize') or f.get('filesize_approx')
                 if abr > best_audio_abr and fsize: best_audio_abr = abr; best_audio_size = fsize
                 elif not best_audio_size and fsize: best_audio_size = fsize # Fallback

        # Get unique video heights and process them
        unique_heights = sorted(list(set(f['height'] for f in formats if f.get('height') and f.get('vcodec') != 'none')), reverse=True)
        for h in unique_heights:
            best_video_size_for_h = 0; best_video_metric = 0 # Metric to find 'best' video at this height
            for f in formats:
                if f.get('height') == h and f.get('vcodec') != 'none':
                    fsize = f.get('filesize') or f.get('filesize_approx')
                    if fsize:
                        # Prefer higher bitrate (vbr/tbr) or larger resolution (width*height)
                        metric = f.get('vbr', 0) or f.get('tbr', 0) or (f.get('width', 0)*f.get('height',0))
                        if metric > best_video_metric: best_video_metric = metric; best_video_size_for_h = fsize
                        elif not best_video_size_for_h: best_video_size_for_h = fsize # Fallback

            total_approx_size = (best_video_size_for_h or 0) + (best_audio_size or 0)
            quality_val = f"{h}p" # Actual value (e.g., "1080p")
            display_text = quality_val # Text shown in dropdown
            if total_approx_size > 0: display_text += f" (≈ {format_bytes(total_approx_size)})"
            else: display_text += " (Size N/A)"
            self.quality_combo.addItem(display_text)
            self.quality_data[display_text] = quality_val # Store mapping: "1080p (...MB)" -> "1080p"

        self.quality_combo.setEnabled(True)

        # --- Populate Format ComboBox ---
        video_exts = set(); has_audio_only = False
        for fmt in formats:
            ext = fmt.get("ext"); vcodec = fmt.get("vcodec", "none"); acodec = fmt.get("acodec", "none")
            if ext and ext.lower() not in ("mhtml", "html"):
                # Check for common video extensions only to avoid clutter? Or allow all? Let's be slightly restrictive.
                if vcodec != "none" and ext.upper() in ["MP4", "MKV", "WEBM", "MOV", "AVI", "FLV"]: video_exts.add(ext.upper())
                if acodec != "none": has_audio_only = True # Does *any* audio exist?

        self.filetype_combo.clear()
        if has_audio_only:
            self.filetype_combo.addItem("Audio Only (MP3)"); self.filetype_combo.addItem("Audio Only (M4A)")
            self.filetype_combo.addItem("Audio Only (OPUS)"); self.filetype_combo.addItem("Audio Only (WAV)")
        preferred_order = ["MP4", "MKV", "WEBM"]; sorted_exts = [p for p in preferred_order if p in video_exts] + sorted([e for e in video_exts if e not in preferred_order])
        if sorted_exts: # Add video options if any suitable were found
            if self.filetype_combo.count() > 0: self.filetype_combo.insertSeparator(self.filetype_combo.count())
            self.filetype_combo.addItem("Best Available") # Default container choice
            self.filetype_combo.addItems(sorted_exts)
            self.filetype_combo.setCurrentText("Best Available") # Select default
        self.filetype_combo.setEnabled(True)

        self.download_button.setEnabled(True)
        self.set_ui_loading_state(False) # Finished loading UI elements

    def on_metadata_error(self, error_message: str):
        """Handles errors during metadata fetching."""
        self.update_log(f"Metadata Error: {error_message}\n")
        self.status_label.setText(f"Error: {error_message}")
        self.status_label.setStyleSheet("color: #FF6B6B;") # Red error color
        self.reset_metadata_ui()
        self.set_ui_loading_state(False) # Re-enable UI but download stays disabled
        # Consider showing critical errors in a popup
        if "yt-dlp command not found" in error_message:
             QMessageBox.warning(self, "yt-dlp Error", error_message)

    def load_thumbnail(self, url: str):
        """Loads thumbnail, includes SSL bypass."""
        try:
            context = ssl._create_unverified_context() # Bypass SSL verification
            # It's generally better to use a library like requests for HTTPS if available,
            # but staying with standard library for simplicity here.
            with urlopen(url, context=context, timeout=10) as response: data = response.read()
            image = QImage(); ok = image.loadFromData(data)
            if ok:
                 pixmap = QPixmap.fromImage(image)
                 scaled_pixmap = pixmap.scaled(self.thumbnail_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                 self.thumbnail_label.setPixmap(scaled_pixmap)
                 self.thumbnail_label.setText("") # Clear placeholder
            else:
                 self.thumbnail_label.setText("Bad Img"); self.thumbnail_label.setPixmap(QPixmap()); self.update_log(f"Decode Err: {url}\n")
        except Exception as e:
             # Catch common errors specifically if needed (SSL, URL, Timeout)
             self.thumbnail_label.setText("Thumb Err"); self.thumbnail_label.setPixmap(QPixmap()); self.update_log(f"Thumb Err {url}: {e}\n")

    def reset_metadata_ui(self):
        """Resets UI elements related to video metadata."""
        self.video_info = {}
        self.quality_data.clear() # Clear the quality mapping
        self.video_title_label.setText("Video title...")
        self.thumbnail_label.setText("Thumbnail...")
        self.thumbnail_label.setPixmap(QPixmap()) # Clear image
        self.quality_combo.clear()
        self.quality_combo.setEnabled(False)
        self.filetype_combo.clear()
        self.filetype_combo.setEnabled(False)
        self.download_button.setEnabled(False)
        self.progress_bar.setValue(0)
        # Don't reset status label text here, let caller do it

    def set_ui_loading_state(self, loading: bool, status_text: str = ""):
         """Enable/disable relevant UI elements during loading."""
         is_ready = not loading and bool(self.video_info) # Ready if not loading AND metadata is present
         # Enable/disable controls based on loading state
         self.url_input.setEnabled(not loading); self.paste_button.setEnabled(not loading); self.clear_button.setEnabled(not loading); self.choose_dir_button.setEnabled(not loading)
         # Enable combos/download only when metadata is successfully loaded (is_ready)
         self.quality_combo.setEnabled(is_ready); self.filetype_combo.setEnabled(is_ready); self.download_button.setEnabled(is_ready)
         # Update status label if loading
         if loading: self.status_label.setText(status_text); self.status_label.setStyleSheet("font-style: italic; color: #cccccc;")

    # --- Download Handling ---
    def start_download(self):
        """Initiates the download process after validation checks."""
        # --- Check 1: Output directory ---
        if not self.output_dir or not os.path.isdir(self.output_dir):
            QMessageBox.warning(self, "Output Folder Required",
                                "Please choose a valid download folder using the 'Browse...' button first.")
            return # Stop the download process
        # --- End check 1 ---

        # --- Check 2: URL and Metadata ---
        url = self.url_input.text().strip()
        if not self._is_valid_url(url) or not self.video_info:
            QMessageBox.warning(self, "Metadata Required", "Load video metadata first using a valid URL.")
            return
        # --- End check 2 ---

        # --- Check 3: Quality and Format Selection ---
        quality_display_text = self.quality_combo.currentText()
        # Use the mapping to get the actual quality value (e.g., "1080p")
        quality = self.quality_data.get(quality_display_text, "Best Available") # Default if not found
        filetype = self.filetype_combo.currentText()
        if not quality or not filetype or not quality_display_text: # Check if selection is valid
             QMessageBox.warning(self, "Selection Required", "Please select valid quality and format options.")
             return
        # --- End check 3 ---

        # --- Check 4: Already Downloading ---
        if self.download_thread and self.download_thread.isRunning():
            QMessageBox.warning(self, "Download in Progress", "Another download is already running.")
            return
        # --- End check 4 ---

        # --- Passed checks, proceed with download ---
        self.set_ui_downloading_state(True) # Disable UI
        self.progress_bar.setValue(0)
        # Reset progress bar style from potential previous error
        current_pb_style = self.progress_bar.styleSheet()
        self.progress_bar.setStyleSheet(current_pb_style.replace("background-color: red;", "background-color: #3498db;"))

        # Log start
        self.update_log(f"Starting Download:\n"
                        f"  Title: {self.video_info.get('title', url)}\n"
                        f"  Quality: {quality} (Selected: '{quality_display_text}')\n"
                        f"  Format: {filetype}\n"
                        f"  Saving To: {self.output_dir}\n"
                        f"{'-'*30}\n")
        self.status_label.setText("Download in progress...")
        self.status_label.setStyleSheet("") # Reset status style

        # Start download thread
        self.download_thread = DownloadThread(url, quality, filetype, self.output_signal, self.output_dir)
        self.download_thread.start()

    def set_ui_downloading_state(self, downloading: bool):
         """Enable/disable UI elements specifically during download."""
         enable = not downloading
         self.download_button.setEnabled(enable)
         self.choose_dir_button.setEnabled(enable)
         self.url_input.setEnabled(enable)
         self.paste_button.setEnabled(enable)
         self.clear_button.setEnabled(enable)
         # Keep combos disabled during download
         self.quality_combo.setEnabled(enable and bool(self.video_info))
         self.filetype_combo.setEnabled(enable and bool(self.video_info))

    def on_download_complete(self, success: bool, message: str):
        """Handles the completion of the download thread."""
        self.set_ui_downloading_state(False) # Re-enable UI
        self.update_log(f"{'-'*30}\n{message}\n")
        self.status_label.setText(message)

        if success:
             self.progress_bar.setValue(100) # Ensure it hits 100%
             self.status_label.setStyleSheet("color: lightgreen;")
        else:
             # Make progress bar chunk red for error
             current_style = self.progress_bar.styleSheet()
             # Avoid adding red repeatedly
             if "background-color: red;" not in current_style:
                  # Be specific about the chunk selector
                  self.progress_bar.setStyleSheet(current_style.replace("background-color: #3498db;", "background-color: red;"))
             self.status_label.setStyleSheet("color: #FF6B6B;") # Red error text

        self.download_thread = None # Clear thread reference

    def update_log(self, text: str):
        """Appends text to the log output area."""
        self.log_output.append(text.strip()) # Append and remove trailing newline
        self.log_output.verticalScrollBar().setValue(self.log_output.verticalScrollBar().maximum()) # Auto-scroll

    def update_progress(self, percent: int):
        """Updates the progress bar value, resetting error color if needed."""
        # Reset error style if progress updates after an error
        current_style = self.progress_bar.styleSheet()
        if "background-color: red;" in current_style:
             self.progress_bar.setStyleSheet(current_style.replace("background-color: red;", "background-color: #3498db;"))
        self.progress_bar.setValue(percent)

    # --- Window Closing ---
    def closeEvent(self, event):
        """Handle window close event."""
        self.metadata_fetch_timer.stop() # Stop any pending metadata fetch
        # Don't explicitly wait for metadata thread, usually fast
        if self.metadata_thread and self.metadata_thread.isRunning(): pass

        if self.download_thread and self.download_thread.isRunning():
             reply = QMessageBox.question(self, 'Confirm Quit',
                                          "A download is running. Stop and quit?",
                                          QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
             if reply == QMessageBox.Yes:
                  self.status_label.setText("Stopping download...")
                  self.download_thread.stop() # Signal thread to stop
                  self.download_thread.wait(1000) # Wait briefly for cleanup
                  event.accept() # Allow closing
             else:
                  event.ignore() # Prevent closing
        else:
            event.accept() # No download running, close normally


# --- Main Execution ---
if __name__ == "__main__":
    # Enable High DPI scaling if available
    if hasattr(Qt, 'AA_EnableHighDpiScaling'): QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'): QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    # QTimer is imported at top level now

    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
