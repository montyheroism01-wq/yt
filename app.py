from flask import Flask, render_template, request, jsonify, send_file, Response
from flask_cors import CORS
import yt_dlp
import os
import sys
import tempfile
import threading
import uuid
import shutil
import time
import socket
import re

# Base directories
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, 'templates')
COOKIES_FILE = os.path.join(PROJECT_ROOT, 'cookies.txt')
DOWNLOADS_DIR = os.path.join(PROJECT_ROOT, 'downloads')
DEFAULT_DOWNLOADS = os.path.join(os.path.expanduser('~'), 'Downloads')
FFMPEG_DIR = os.path.join(PROJECT_ROOT, 'ffmpeg')

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(FFMPEG_DIR, exist_ok=True)

# Write cookies from environment variable if provided on cloud hosts (Render/Railway)
ENV_COOKIES = os.environ.get('YOUTUBE_COOKIES', '').strip()
if ENV_COOKIES and not os.path.isfile(COOKIES_FILE):
    try:
        with open(COOKIES_FILE, 'w', encoding='utf-8') as f:
            f.write(ENV_COOKIES)
        print("[Auth] Created cookies.txt from YOUTUBE_COOKIES environment variable.")
    except Exception as e:
        print(f"[Auth Error] Could not write cookies file: {e}")

# Add local tools (FFmpeg & Deno) to PATH if present
if os.path.exists(FFMPEG_DIR):
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")
    print(f"[Engine] Bundled tools (FFmpeg & Deno) active from: {FFMPEG_DIR}")

app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.secret_key = 'bluestream_pro_secret_key_v10'
CORS(app)

progress_data = {}
downloaded_files = {}
lock = threading.Lock()

# Clean all ANSI color codes and control characters
ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\([B0-9A-Za-z]|\033\[[0-9;]*m')

def clean_ansi(text):
    """Remove terminal color codes and control characters"""
    if not text:
        return ''
    cleaned = ANSI_ESCAPE_RE.sub('', str(text))
    cleaned = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', cleaned)
    return cleaned.strip()

VALID_MEDIA_EXTENSIONS = ('.mp4', '.mkv', '.webm', '.mp3', '.m4a', '.wav', '.opus', '.flac', '.aac')

def get_local_ip():
    """Get the local network IP for mobile connections"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def is_mobile():
    """Check if the requesting client is a mobile device"""
    user_agent = request.headers.get('User-Agent', '').lower()
    mobile_keywords = ['mobile', 'android', 'iphone', 'ipad', 'ipod', 'windows phone', 'blackberry']
    return any(keyword in user_agent for keyword in mobile_keywords)

def parse_time_to_seconds(val):
    """Convert string time (HH:MM:SS or MM:SS or seconds) to float seconds"""
    if val is None or str(val).strip() == '':
        return None
    if isinstance(val, (int, float)):
        return float(val)
    val_str = str(val).strip()
    parts = val_str.split(':')
    try:
        if len(parts) == 1:
            return float(parts[0])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None
    return None

def format_seconds_to_str(seconds):
    """Convert seconds to HH:MM:SS or MM:SS format"""
    if seconds is None:
        return "00:00"
    s = int(seconds)
    hours = s // 3600
    minutes = (s % 3600) // 60
    secs = s % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"

def get_base_ydl_opts():
    opts = {
        'quiet': True,
        'no_warnings': True,
        'writethumbnail': False,
        'writeinfojson': False,
        'remote_components': ['ejs:github'],
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android', 'web_embedded', 'mweb'],
            }
        },
    }
    if os.path.exists(FFMPEG_DIR):
        opts['ffmpeg_location'] = FFMPEG_DIR
        
    if os.path.isfile(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE
        
    return opts

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    try:
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'extract_flat': 'in_playlist',
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            is_playlist = 'entries' in info and bool(info['entries'])
            
            if is_playlist:
                valid_entries = [e for e in info['entries'] if e is not None and isinstance(e, dict)]
                first_entry = valid_entries[0] if valid_entries else {}
                thumbnail = info.get('thumbnail') or first_entry.get('thumbnail') or ''
                return jsonify({
                    'type': 'playlist',
                    'title': info.get('title', 'YouTube Playlist'),
                    'uploader': info.get('uploader', 'Unknown Creator'),
                    'thumbnail': thumbnail,
                    'is_playlist': True,
                    'count': len(valid_entries),
                    'duration_seconds': 0,
                    'duration': f"{len(valid_entries)} Videos",
                    'qualities': [2160, 1440, 1080, 720, 480, 360]
                })
            else:
                formats = info.get('formats', [])
                heights = sorted(list(set(f.get('height', 0) for f in formats if f.get('height'))), reverse=True)
                qualities = [h for h in heights if h > 0]
                if not qualities:
                    qualities = [2160, 1440, 1080, 720, 480, 360]
                    
                duration_sec = info.get('duration', 0) or 0
                duration_str = format_seconds_to_str(duration_sec)

                return jsonify({
                    'type': 'video',
                    'title': info.get('title', 'YouTube Video'),
                    'uploader': info.get('uploader', 'Unknown Creator'),
                    'thumbnail': info.get('thumbnail', ''),
                    'duration': duration_str,
                    'duration_seconds': duration_sec,
                    'is_playlist': False,
                    'qualities': qualities,
                    'best_quality': f"{qualities[0]}p" if qualities else "1080p"
                })
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/browse')
def browse():
    user_home = os.path.expanduser('~')
    folders = [
        {'name': 'Downloads', 'path': os.path.join(user_home, 'Downloads'), 'icon': '📥'},
        {'name': 'Desktop', 'path': os.path.join(user_home, 'Desktop'), 'icon': '🖥️'},
        {'name': 'Videos', 'path': os.path.join(user_home, 'Videos'), 'icon': '🎬'},
        {'name': 'Music', 'path': os.path.join(user_home, 'Music'), 'icon': '🎵'},
        {'name': 'Documents', 'path': os.path.join(user_home, 'Documents'), 'icon': '📄'},
    ]
    return jsonify({
        'default': DEFAULT_DOWNLOADS,
        'folders': folders,
        'is_mobile': is_mobile()
    })

@app.route('/download', methods=['POST'])
def download():
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    media_type = data.get('type', 'video')
    quality = data.get('quality')
    
    # Trimming options
    trim_enabled = data.get('trim_enabled', False)
    start_time_val = data.get('start_time')
    end_time_val = data.get('end_time')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    task_id = str(uuid.uuid4())
    mobile_client = is_mobile()

    # Parse trim times if enabled
    start_sec = parse_time_to_seconds(start_time_val) if trim_enabled else None
    end_sec = parse_time_to_seconds(end_time_val) if trim_enabled else None

    # Check if this is an actual partial range slice
    is_trimmed = bool(trim_enabled and (start_sec is not None or end_sec is not None))

    with lock:
        progress_data[task_id] = {
            'status': 'starting',
            'progress': 0,
            'speed': 'Connecting to YouTube...',
            'eta': '--:--',
            'filename': '',
            'is_mobile': mobile_client,
            'is_trimmed': is_trimmed
        }

    def download_thread():
        task_dir = os.path.join(DOWNLOADS_DIR, task_id)
        os.makedirs(task_dir, exist_ok=True)
        trim_stop_event = threading.Event()

        # Dynamic progress animator for trimming
        def trim_progress_animator():
            stages = [
                (25.0, "✂️ Direct streaming requested section...", "A few seconds"),
                (55.0, "⚡ Lossless stream copy muxing...", "00:04"),
                (80.0, "🎬 Finalizing MP4 container...", "00:02"),
                (95.0, "✨ Packaging download for browser...", "00:01")
            ]
            for pct, spd, eta in stages:
                if trim_stop_event.is_set():
                    break
                with lock:
                    if progress_data[task_id]['status'] in ['starting', 'processing', 'downloading']:
                        progress_data[task_id]['progress'] = pct
                        progress_data[task_id]['speed'] = spd
                        progress_data[task_id]['eta'] = eta
                        progress_data[task_id]['status'] = 'processing'
                time.sleep(1.8)

        if is_trimmed:
            threading.Thread(target=trim_progress_animator, daemon=True).start()

        try:
            final_file_container = {'path': None}

            def progress_hook(d):
                if d['status'] == 'downloading':
                    total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    downloaded_bytes = d.get('downloaded_bytes', 0)
                    
                    if total_bytes > 0:
                        pct = round((downloaded_bytes / total_bytes) * 100, 1)
                    else:
                        raw_pct = clean_ansi(d.get('_percent_str', '0%')).replace('%', '').strip()
                        try:
                            pct = float(raw_pct)
                        except ValueError:
                            pct = 0.0

                    speed_clean = clean_ansi(d.get('_speed_str', ''))
                    eta_clean = clean_ansi(d.get('_eta_str', ''))

                    with lock:
                        if not is_trimmed or pct > progress_data[task_id].get('progress', 0):
                            progress_data[task_id]['progress'] = pct
                        if speed_clean:
                            progress_data[task_id]['speed'] = speed_clean
                        if eta_clean:
                            progress_data[task_id]['eta'] = eta_clean
                        progress_data[task_id]['status'] = 'downloading'
                        if d.get('filename'):
                            fname = os.path.basename(d['filename'])
                            progress_data[task_id]['filename'] = clean_ansi(fname)

                elif d['status'] == 'finished':
                    with lock:
                        progress_data[task_id]['status'] = 'processing'
                        progress_data[task_id]['progress'] = 98.0
                    if d.get('filename') and d['filename'].lower().endswith(VALID_MEDIA_EXTENSIONS):
                        final_file_container['path'] = d['filename']

            def postprocessor_hook(d):
                if d.get('status') == 'finished':
                    info_dict = d.get('info_dict', {})
                    fp = info_dict.get('filepath') or d.get('filepath')
                    if fp and os.path.exists(fp) and fp.lower().endswith(VALID_MEDIA_EXTENSIONS):
                        final_file_container['path'] = fp

            ydl_opts = get_base_ydl_opts()
            
            # Form filename template
            if is_trimmed:
                s_label = format_seconds_to_str(start_sec or 0).replace(':', '_')
                e_label = format_seconds_to_str(end_sec).replace(':', '_') if end_sec else 'end'
                filename_template = f'%(title)s [trim {s_label}-{e_label}].%(ext)s'
            else:
                filename_template = '%(title)s.%(ext)s'

            ydl_opts.update({
                'outtmpl': os.path.join(task_dir, filename_template),
                'progress_hooks': [progress_hook],
                'postprocessor_hooks': [postprocessor_hook],
                'windowsfilenames': True,
            })

            # Stream ONLY requested section
            if is_trimmed:
                def section_picker(info_dict, ydl_instance):
                    start = start_sec if start_sec is not None else 0
                    end = end_sec if end_sec is not None else (info_dict.get('duration') or 999999)
                    return [{'start_time': start, 'end_time': end, 'title': 'trimmed_section'}]
                
                ydl_opts['download_ranges'] = section_picker
                ydl_opts['force_keyframes_at_cuts'] = False

            if media_type == 'audio':
                ydl_opts.update({
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '320',
                    }],
                })
            else:  # video
                if quality:
                    ydl_opts['format'] = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best'
                else:
                    ydl_opts['format'] = 'bestvideo+bestaudio/best'
                ydl_opts['merge_output_format'] = 'mp4'

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            trim_stop_event.set()

            # Locate final output file
            found_filepath = final_file_container['path']
            if not found_filepath or not os.path.exists(found_filepath):
                media_files = [
                    os.path.join(task_dir, f) for f in os.listdir(task_dir)
                    if os.path.isfile(os.path.join(task_dir, f)) and f.lower().endswith(VALID_MEDIA_EXTENSIONS)
                ]
                if media_files:
                    media_files.sort(key=os.path.getmtime, reverse=True)
                    found_filepath = media_files[0]

            if not found_filepath or not os.path.exists(found_filepath):
                raise Exception("Download completed but media file could not be generated.")

            actual_name = os.path.basename(found_filepath)

            with lock:
                downloaded_files[task_id] = {
                    'filepath': found_filepath,
                    'filename': actual_name,
                    'task_dir': task_dir,
                    'is_mobile': mobile_client,
                }

                progress_data[task_id]['status'] = 'completed'
                progress_data[task_id]['progress'] = 100
                progress_data[task_id]['filename'] = actual_name
                progress_data[task_id]['speed'] = 'Ready!'
                progress_data[task_id]['eta'] = '00:00'

        except Exception as e:
            trim_stop_event.set()
            print(f"[Error downloading]: {e}")
            with lock:
                progress_data[task_id]['status'] = 'error'
                progress_data[task_id]['error'] = clean_ansi(str(e))

    threading.Thread(target=download_thread, daemon=True).start()
    return jsonify({'task_id': task_id, 'is_mobile': mobile_client})

@app.route('/progress/<task_id>')
def progress(task_id):
    with lock:
        data = progress_data.get(task_id, {'status': 'not found'})
        res = dict(data)
        if data.get('status') == 'completed':
            file_info = downloaded_files.get(task_id)
            if file_info and file_info.get('filepath') and os.path.exists(file_info['filepath']):
                res['download_url'] = f'/file/{task_id}'
                res['filename'] = file_info.get('filename')
    return jsonify(res)

@app.route('/file/<task_id>')
def file_download(task_id):
    with lock:
        file_info = downloaded_files.get(task_id)
    if not file_info or not file_info.get('filepath'):
        return jsonify({'error': 'File not found'}), 404

    filepath = file_info['filepath']
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found on server'}), 404

    try:
        response = send_file(
            filepath,
            as_attachment=True,
            download_name=file_info.get('filename', os.path.basename(filepath))
        )
        
        if file_info.get('task_dir'):
            @response.call_on_close
            def cleanup():
                try:
                    time.sleep(5)
                    shutil.rmtree(file_info['task_dir'], ignore_errors=True)
                    with lock:
                        if task_id in downloaded_files:
                            del downloaded_files[task_id]
                except Exception as e:
                    print(f"Error cleaning up task dir: {e}")

        return response
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    local_ip = get_local_ip()
    print("=" * 65)
    print("      🌟 BLUESTREAM PRO V10 - 4K MEDIA & SLICE ENGINE ONLINE")
    print(f"  [+] Local Web Access:    http://localhost:{port}")
    print(f"  [+] Mobile Wi-Fi Access: http://{local_ip}:{port}")
    print(f"  [+] True 4K Engine:      ACTIVE (Multi-Client Bypass)")
    print(f"  [+] Fast Lossless Cut:   ACTIVE (Lossless Stream Copy)")
    print("=" * 65)
    app.run(host='0.0.0.0', port=port, debug=False)