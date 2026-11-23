# GDrive CLI Downloader

A lightweight, fast, multi-threaded, and resumable Google Drive video and file downloader written in Python.

---

## Features

- **Multi-threaded Downloads**: Splits files across multiple concurrent threads (1 to 16) with HTTP `Range` requests for maximum download speeds.
- **Resumable Transfers**: If interrupted, it automatically detects existing bytes in `.part` files and resumes without restarting from zero.
- **High-Throughput Buffer**: Uses optimized chunk buffers (default 256 KB) to prevent CPU bottlenecks on large media files.
- **Session & Cookie Support**: Works with Netscape `cookies.txt` or JSON cookie dumps (for private/restricted drives).
- **Graceful Error Handling**: Captures `Ctrl+C` (KeyboardInterrupt) safely to preserve partial progress on disk.
- **Cross-Platform**: Automatic filename sanitization for Windows and Linux.

---

## Installation

Ensure you have Python 3.10+ installed, then install the dependencies:

```bash
pip install -r requirements.txt
```

*(Dependencies: `requests`, `tqdm`)*

---

## Usage

### 1. Basic Download
Provide either the full Google Drive link or the raw file ID:

```bash
python downloader.py "https://drive.google.com/file/d/1A2B3C4D5E.../view"
```

Or by File ID:
```bash
python downloader.py 1A2B3C4D5E...
```

### 2. Specify Output File & Directory

```bash
python downloader.py "<LINK_OR_ID>" -o "lecture_1.mp4" -d "./videos"
```

### 3. Adjust Concurrency & Buffer Size
Increase threads (up to 16) and buffer size (in KB):

```bash
python downloader.py "<LINK_OR_ID>" -t 8 -c 512
```

### 4. Authenticated / Restricted Files (Using Cookies)
For files requiring login or organizational access:
1. Export cookies from your browser using an extension (such as *Get cookies.txt LOCALLY* or *Cookie-Editor*).
2. Save as `cookies.txt` (Netscape format) or `cookies.json`.
3. Pass via `--cookies`:

```bash
python downloader.py "<LINK_OR_ID>" --cookies "cookies.txt"
```

---

## Command Options

| Argument | Description | Default |
|---|---|---|
| `url_or_id` | Google Drive link or File ID | *(Required)* |
| `-o`, `--output` | Custom output filename | Auto-detected from Drive |
| `-d`, `--dir` | Destination directory | `.` (Current directory) |
| `-t`, `--threads` | Number of worker threads (1–16) | `4` |
| `-c`, `--chunk-size` | Chunk buffer size in KB | `256` |
| `--cookies` | Path to Netscape `.txt` or JSON cookie file | `None` |
| `-v`, `--verbose` | Show verbose debugging details | Disabled |
