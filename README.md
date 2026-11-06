# OmniStream Studio 🎬

**Created by Angelo Golloberda**

OmniStream Studio is a sleek, modern desktop media downloader built with **Python**, **PyQt5**, and **yt-dlp**. It provides a studio-grade dark interface for downloading high-resolution videos, audio tracks, YouTube Mixes, and full playlists with real-time HUD metrics.

---

## ✨ Features

- **📚 Full YouTube Mix & Playlist Support**:
  - Automatically loads and tracks full YouTube mixes and playlists.
  - **Download Scope Selector**: Download the entire mix, top 25/50/100 tracks, or just the single video.
  - Resilient batch downloads: automatically skips private or region-blocked tracks without aborting the queue.
  - Organized output: tracks are saved cleanly into numbered folder structures (`[Playlist Name]/01 - Track.mp4`).

- **🎧 Comprehensive Format Support**:
  - **Video**: Universal MP4, High-Definition MKV, and Best Available streams (up to 4K/1080p/720p).
  - **Audio Only**: High-Quality MP3, Apple AAC (M4A), and OPUS.

- **🎨 Modern Dark UI**:
  - Engineered with a responsive layout, custom chevron controls, and status pills.
  - Author watermark and studio aesthetics.

- **📊 Real-Time HUD & Terminal Console**:
  - Live speed, ETA, file size, percentage bar, and current track indicator (`Item X/Y`).
  - Integrated activity log for deep inspection.

- **🍪 Browser Cookie Integration**:
  - Pass authenticated cookies from Chrome, Firefox, Edge, Brave, or custom cookie files to download private or age-restricted content.

---

## 🚀 Getting Started

### Prerequisites

1. **Python 3.10+** (Recommended: Python 3.11)
2. **yt-dlp** and **FFmpeg**:
   Using Windows Package Manager (cmd / PowerShell):
   ```bash
   winget install yt-dlp
   winget install Gyan.FFmpeg
   ```

### Installation

1. Clone or download this repository:
   ```bash
   git clone https://github.com/<your-username>/OmniStream-Studio.git
   cd OmniStream-Studio
   ```

2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Launch the application:
   ```bash
   python Youtube_Downloader.py
   ```

---

## 🛠️ Building the Standalone Executable

To bundle the application into a standalone Windows `.exe` using PyInstaller:

```bash
pip install pyinstaller
python -m PyInstaller --noconsole --onefile --name OmniStream_Studio Youtube_Downloader.py
```

---

## 👤 Author

Developed by **Angelo Golloberda**
