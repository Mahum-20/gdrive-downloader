#!/usr/bin/env python3
"""
GDrive CLI Downloader
A fast, multi-threaded, resumable Google Drive video & file downloader.
"""

import os
import re
import sys
import math
import json
import shutil
import urllib.parse
import http.cookiejar
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse

import requests
from tqdm import tqdm

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def sanitize_filename(name: str) -> str:
    """Removes invalid characters for Windows and Linux filesystem safety."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', name)
    cleaned = cleaned.strip('. ')
    return cleaned if cleaned else "downloaded_video.mp4"


def extract_drive_id(text: str) -> str:
    """Extracts the Google Drive file ID from a URL or returns the input if already an ID."""
    text = text.strip()
    match = re.search(r'/file/d/([a-zA-Z0-9_-]+)', text)
    if match:
        return match.group(1)
    match = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', text)
    if match:
        return match.group(1)
    match = re.search(r'drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)', text)
    if match:
        return match.group(1)
    # Check if raw ID
    if re.fullmatch(r'[a-zA-Z0-9_-]{25,}', text):
        return text
    return text


def load_cookies(filepath: str, session: requests.Session, verbose: bool = False):
    """Loads cookies from a Netscape txt or JSON file into the session."""
    if not os.path.isfile(filepath):
        print(f"[!] Cookies file not found: {filepath}", file=sys.stderr)
        return

    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        first_char = f.read(1)
        f.seek(0)
        content = f.read()

    # JSON format export (common from Chrome/Firefox extensions)
    if first_char in ('[', '{'):
        try:
            cookie_data = json.loads(content)
            if isinstance(cookie_data, dict):
                cookie_data = cookie_data.get('cookies', [])
            count = 0
            for item in cookie_data:
                name = item.get('name')
                value = item.get('value')
                domain = item.get('domain', '.google.com')
                path = item.get('path', '/')
                if name and value:
                    session.cookies.set(name, value, domain=domain, path=path)
                    count += 1
            if verbose:
                print(f"[*] Loaded {count} cookies from JSON file.")
            return
        except Exception as e:
            if verbose:
                print(f"[!] Failed to parse JSON cookies: {e}")

    # Netscape format
    try:
        temp_file = None
        target_path = filepath
        if not content.startswith("# Netscape HTTP Cookie File"):
            temp = tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8')
            temp.write("# Netscape HTTP Cookie File\n" + content)
            temp.close()
            temp_file = temp.name
            target_path = temp_file

        jar = http.cookiejar.MozillaCookieJar(target_path)
        jar.load(ignore_discard=True, ignore_expires=True)
        for cookie in jar:
            session.cookies.set_cookie(cookie)
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)
        if verbose:
            print(f"[*] Loaded cookies from Netscape file.")
    except Exception as e:
        print(f"[!] Error loading cookies from {filepath}: {e}", file=sys.stderr)


def get_video_info_and_streams(session: requests.Session, file_id: str, verbose: bool = False):
    """
    Retrieves video metadata and all direct playback streams from Google Drive,
    sorted by quality (highest resolution first).
    """
    info_url = f"https://drive.google.com/u/0/get_video_info?docid={file_id}&drive_originator_app=303"
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Referer": f"https://drive.google.com/file/d/{file_id}/view"
    }

    streams = []
    title = None

    try:
        resp = session.get(info_url, headers=headers, timeout=15)
        if resp.status_code == 200:
            parsed = urllib.parse.parse_qs(resp.text)
            status = parsed.get("status", [""])[0]

            if status != "fail":
                title = parsed.get("title", [None])[0]

                # 1. Parse player_response (contains exact resolutions and stream URLs)
                if "player_response" in parsed:
                    try:
                        pr = json.loads(parsed["player_response"][0])
                        streaming_data = pr.get("streamingData", {})
                        for fmt in streaming_data.get("formats", []):
                            if "url" in fmt:
                                h = fmt.get("height", 0)
                                w = fmt.get("width", 0)
                                q_label = fmt.get("qualityLabel") or (f"{h}p" if h else "unknown")
                                streams.append({
                                    "itag": fmt.get("itag"),
                                    "quality": q_label,
                                    "height": h,
                                    "width": w,
                                    "mime": fmt.get("mimeType", "video/mp4"),
                                    "url": fmt["url"]
                                })
                    except Exception as e:
                        if verbose:
                            print(f"[!] Error parsing player_response JSON: {e}")

                # 2. Check fmt_stream_map & fmt_list if player_response had no formats
                if not streams:
                    fmt_resolutions = {}
                    if "fmt_list" in parsed:
                        for item in parsed["fmt_list"][0].split(","):
                            parts = item.split("/")
                            if len(parts) >= 2:
                                dims = parts[1].split("x")
                                if len(dims) == 2 and dims[1].isdigit():
                                    fmt_resolutions[parts[0]] = (int(dims[0]), int(dims[1]))

                    for key in ["url_encoded_fmt_stream_map", "fmt_stream_map"]:
                        if key in parsed:
                            for s in parsed[key][0].split(","):
                                sub_params = urllib.parse.parse_qs(s)
                                if "url" in sub_params:
                                    itag = sub_params.get("itag", [""])[0]
                                    w, h = fmt_resolutions.get(itag, (0, 0))
                                    q = f"{h}p" if h else sub_params.get("quality", ["unknown"])[0]
                                    streams.append({
                                        "itag": itag,
                                        "quality": q,
                                        "height": h,
                                        "width": w,
                                        "mime": sub_params.get("type", ["video/mp4"])[0],
                                        "url": sub_params["url"][0]
                                    })
                            if streams:
                                break

                # 3. Direct videoplayback stream in raw text
                if not streams:
                    for part in resp.text.split("&"):
                        if "videoplayback" in part:
                            key_val = part.split("=", 1)
                            if len(key_val) == 2:
                                potential_url = urllib.parse.unquote(key_val[1])
                                if potential_url.startswith("http"):
                                    streams.append({
                                        "itag": None,
                                        "quality": "stream",
                                        "height": 0,
                                        "width": 0,
                                        "mime": "video/mp4",
                                        "url": potential_url
                                    })
                                    break
            elif verbose:
                reason = parsed.get("reason", ["Unknown"])[0]
                print(f"[*] get_video_info returned status={status}: {reason}")
    except Exception as e:
        if verbose:
            print(f"[!] Error probing get_video_info: {e}")

    # Fallback to direct export download URL
    if not streams:
        fallback_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        streams.append({
            "itag": None,
            "quality": "source/export",
            "height": 0,
            "width": 0,
            "mime": "video/mp4",
            "url": fallback_url
        })

    # Sort streams by resolution height descending (highest quality first)
    streams.sort(key=lambda x: x.get("height", 0), reverse=True)
    return title or f"{file_id}.mp4", streams


def is_html_response(resp: requests.Response) -> bool:
    content_type = resp.headers.get("Content-Type", "").lower()
    return "text/html" in content_type


def probe_file_size(session: requests.Session, url: str) -> tuple[int, bool]:
    """
    Probes Content-Length and Range request support.
    Returns (total_size, supports_range).
    """
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": "https://drive.google.com/"
    }

    # First try HEAD
    try:
        r = session.head(url, headers=headers, allow_redirects=True, timeout=10)
        if is_html_response(r):
            return 0, False
        size = int(r.headers.get("Content-Length", 0))
        accept_ranges = r.headers.get("Accept-Ranges", "").lower() == "bytes"
        if size > 0:
            return size, accept_ranges
    except Exception:
        pass

    # Fallback: Ranged GET byte 0-0
    try:
        r_headers = headers.copy()
        r_headers["Range"] = "bytes=0-0"
        r = session.get(url, headers=r_headers, stream=True, allow_redirects=True, timeout=10)
        if is_html_response(r):
            return 0, False
        content_range = r.headers.get("Content-Range", "")
        if "/" in content_range:
            total_str = content_range.split("/")[-1]
            if total_str.isdigit():
                return int(total_str), True
        size = int(r.headers.get("Content-Length", 0))
        return size, False
    except Exception:
        return 0, False


def download_chunk(
    session: requests.Session,
    url: str,
    start_byte: int,
    end_byte: int,
    part_filepath: str,
    chunk_buffer_size: int,
    progress_bar: tqdm,
    progress_lock: threading.Lock,
    cancel_event: threading.Event
):
    """Downloads a specific byte range to a .part file with resume support."""
    downloaded = 0
    if os.path.exists(part_filepath):
        downloaded = os.path.getsize(part_filepath)

    actual_start = start_byte + downloaded
    if actual_start > end_byte:
        # Part is already fully downloaded
        with progress_lock:
            progress_bar.update(downloaded)
        return

    # Update progress bar with existing bytes
    if downloaded > 0:
        with progress_lock:
            progress_bar.update(downloaded)

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": "https://drive.google.com/",
        "Range": f"bytes={actual_start}-{end_byte}"
    }

    mode = "ab" if downloaded > 0 else "wb"
    with session.get(url, headers=headers, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(part_filepath, mode) as f:
            for chunk in r.iter_content(chunk_size=chunk_buffer_size):
                if cancel_event.is_set():
                    return
                if chunk:
                    f.write(chunk)
                    with progress_lock:
                        progress_bar.update(len(chunk))


def merge_parts(output_path: str, part_files: list[str]):
    """Merges all temporary part files into the final destination."""
    with open(output_path, "wb") as outfile:
        for p in part_files:
            with open(p, "rb") as infile:
                shutil.copyfileobj(infile, outfile, length=1024 * 1024)
            try:
                os.remove(p)
            except OSError:
                pass


def download_single_stream(
    session: requests.Session,
    url: str,
    output_path: str,
    chunk_buffer_size: int,
    total_size: int
):
    """Fallback single-threaded streaming download."""
    downloaded = 0
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": "https://drive.google.com/"
    }

    if os.path.exists(output_path):
        downloaded = os.path.getsize(output_path)
        if total_size and downloaded >= total_size:
            print(f"[✓] File already downloaded: {output_path}")
            return
        headers["Range"] = f"bytes={downloaded}-"

    mode = "ab" if downloaded > 0 else "wb"
    with session.get(url, headers=headers, stream=True, timeout=30) as r:
        r.raise_for_status()
        if is_html_response(r):
            raise PermissionError(
                "Google Drive returned an HTML page (sign-in or access denied) instead of a video stream. "
                "This file requires authentication. Please export your browser cookies and pass --cookies <path>."
            )
        with open(output_path, mode) as f, tqdm(
            total=total_size,
            initial=downloaded,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=os.path.basename(output_path)
        ) as bar:
            for chunk in r.iter_content(chunk_size=chunk_buffer_size):
                if chunk:
                    f.write(chunk)
                    bar.update(len(chunk))


def main():
    parser = argparse.ArgumentParser(
        description="GDrive Video & File CLI Downloader - Fast, multi-threaded, and resumable."
    )
    parser.add_argument("url_or_id", help="Google Drive link or File ID")
    parser.add_argument("-o", "--output", help="Custom output filename", default=None)
    parser.add_argument("-d", "--dir", help="Output directory path", default=".")
    parser.add_argument("-t", "--threads", type=int, default=4, help="Number of download threads (1-16, default: 4)")
    parser.add_argument("-c", "--chunk-size", type=int, default=256, help="Chunk buffer size in KB (default: 256)")
    parser.add_argument("-q", "--quality", default="best", help="Desired video quality (e.g. best, 1080p, 720p, 480p, 360p). Default: best")
    parser.add_argument("-F", "--list-formats", action="store_true", help="List available video formats/resolutions and exit")
    parser.add_argument("--cookies", help="Path to cookies file (Netscape .txt or JSON)", default=None)
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug output")

    args = parser.parse_args()

    # Enforce thread bounds
    threads = max(1, min(args.threads, 16))
    chunk_buffer_size = max(16, args.chunk_size) * 1024

    # Prepare session
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

    if args.cookies:
        load_cookies(args.cookies, session, verbose=args.verbose)

    # Resolve target ID
    file_id = extract_drive_id(args.url_or_id)
    if args.verbose:
        print(f"[*] Target File ID: {file_id}")

    # Create output directory
    out_dir = os.path.abspath(args.dir)
    os.makedirs(out_dir, exist_ok=True)

    print(f"[*] Resolving video stream metadata...")
    title, streams = get_video_info_and_streams(session, file_id, verbose=args.verbose)

    # Handle format listing flag
    if args.list_formats:
        print(f"\nAvailable formats for '{title}':")
        print(f"{'Quality':<12} {'Resolution':<14} {'ITAG':<8} {'Format'}")
        print("-" * 52)
        for s in streams:
            res = f"{s['width']}x{s['height']}" if s['height'] else "auto"
            itag = str(s['itag']) if s['itag'] else "-"
            mime = s['mime'].split(';')[0] if s['mime'] else "video/mp4"
            print(f"{s['quality']:<12} {res:<14} {itag:<8} {mime}")
        return

    # Select desired stream
    selected_stream = None
    req_q = args.quality.lower().strip()

    if req_q == "best":
        selected_stream = streams[0]
    else:
        for s in streams:
            q_label = str(s["quality"]).lower()
            h_str = str(s["height"])
            if req_q in (q_label, h_str, f"{h_str}p"):
                selected_stream = s
                break
        if not selected_stream:
            print(f"[!] Quality '{args.quality}' not found. Defaulting to best available ({streams[0]['quality']}).")
            selected_stream = streams[0]

    stream_url = selected_stream["url"]
    q_display = selected_stream['quality']
    if selected_stream['width'] and selected_stream['height']:
        q_display += f" ({selected_stream['width']}x{selected_stream['height']})"
    print(f"[*] Selected Quality: {q_display}")

    # Determine filename
    filename = args.output if args.output else title
    filename = sanitize_filename(filename)
    if not os.path.splitext(filename)[1]:
        filename += ".mp4"
    output_path = os.path.join(out_dir, filename)

    print(f"[*] Target: {filename}")
    print(f"[*] Probing stream size...")
    total_size, supports_range = probe_file_size(session, stream_url)

    if total_size > 0:
        size_mb = total_size / (1024 * 1024)
        print(f"[*] Total Size: {size_mb:.2f} MB ({total_size:,} bytes)")
    else:
        print("[!] File size could not be determined in advance.")

    # Check if file is already complete
    if os.path.exists(output_path) and total_size > 0:
        if os.path.getsize(output_path) == total_size:
            print(f"[✓] File already completed: {output_path}")
            return

    # Decide download strategy
    if threads > 1 and supports_range and total_size > 0:
        print(f"[*] Multi-threaded download started using {threads} threads...")
        part_size = math.ceil(total_size / threads)
        part_files = []
        ranges = []

        for i in range(threads):
            start = i * part_size
            end = min(total_size - 1, (i + 1) * part_size - 1)
            part_path = f"{output_path}.part{i}"
            part_files.append(part_path)
            ranges.append((start, end, part_path))

        progress_lock = threading.Lock()
        cancel_event = threading.Event()

        with tqdm(
            total=total_size,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=filename
        ) as bar:
            try:
                with ThreadPoolExecutor(max_workers=threads) as executor:
                    futures = [
                        executor.submit(
                            download_chunk,
                            session,
                            stream_url,
                            start,
                            end,
                            p_path,
                            chunk_buffer_size,
                            bar,
                            progress_lock,
                            cancel_event
                        )
                        for start, end, p_path in ranges
                    ]
                    for future in as_completed(futures):
                        future.result()

                print("\n[*] Assembling downloaded parts...")
                merge_parts(output_path, part_files)
                print(f"[✓] Download completed successfully: {output_path}")

            except KeyboardInterrupt:
                cancel_event.set()
                print("\n[!] Download interrupted by user. Progress saved, run again to resume.")
                sys.exit(1)
            except Exception as e:
                cancel_event.set()
                print(f"\n[!] Download failed: {e}", file=sys.stderr)
                sys.exit(1)
    else:
        print("[*] Starting single-stream download...")
        try:
            download_single_stream(session, stream_url, output_path, chunk_buffer_size, total_size)
            print(f"\n[✓] Download completed successfully: {output_path}")
        except KeyboardInterrupt:
            print("\n[!] Download interrupted by user.")
            sys.exit(1)
        except Exception as e:
            print(f"\n[!] Download failed: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
