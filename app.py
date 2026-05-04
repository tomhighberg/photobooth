"""
PhotoBooth — app.py
Flask application serving the booth UI, admin dashboard, gallery,
projector display, and all API endpoints.

Hardware calls (camera, printer, GPIO) are stubbed when MOCK=True
so you can develop and test the full flow on a laptop.
Flip MOCK=False on the Pi to activate real hardware.
"""

import os
import json
import time
import uuid
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, send_from_directory, jsonify, request, abort

from composite import build_single, build_strip

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════

MOCK = False                       # Flip to False on the Pi
USE_WEBCAM = True   # Set False when D3400 is connected

BASE_DIR   = Path(__file__).parent
PHOTOS_DIR = BASE_DIR / 'photos'
OVERLAYS_DIR = BASE_DIR / 'overlays'
STATIC_DIR = BASE_DIR / 'static'
TEMPLATES_DIR = BASE_DIR / 'templates'
CONFIG_FILE = BASE_DIR / 'config.json'

PHOTOS_DIR.mkdir(exist_ok=True)
OVERLAYS_DIR.mkdir(exist_ok=True)

# ══════════════════════════════════════════════════════════════
#  HARDWARE ABSTRACTION
#  Each function has a real path and a mock path.
#  The mock path generates placeholder images / logs to console.
# ══════════════════════════════════════════════════════════════

if not MOCK:
    try:
        import gphoto2 as gp
        import cups
        import RPi.GPIO as GPIO
    except ImportError as e:
        print(f"[WARN] Hardware library not available: {e}")
        print("[WARN] Falling back to MOCK mode")
        MOCK = True

# ── GPIO ──

FLASH_PIN    = 17
INDICATOR_PIN = 18
SHUTDOWN_PIN = 27

def gpio_setup():
    if MOCK:
        print("[MOCK] GPIO setup — pins 17 (flash), 18 (indicator), 27 (shutdown)")
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(FLASH_PIN, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(INDICATOR_PIN, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(SHUTDOWN_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def flash_on():
    if MOCK:
        print("[MOCK] Flash ON")
        return
    GPIO.output(FLASH_PIN, GPIO.HIGH)

def flash_off():
    if MOCK:
        print("[MOCK] Flash OFF")
        return
    GPIO.output(FLASH_PIN, GPIO.LOW)

def indicator_set(on: bool):
    if MOCK:
        print(f"[MOCK] Indicator LED {'ON' if on else 'OFF'}")
        return
    GPIO.output(INDICATOR_PIN, GPIO.HIGH if on else GPIO.LOW)

def shutdown_monitor():
    """Background thread — polls shutdown button, triggers graceful shutdown."""
    while True:
        if MOCK:
            time.sleep(5)
            continue
        if GPIO.input(SHUTDOWN_PIN) == GPIO.LOW:
            time.sleep(3)
            if GPIO.input(SHUTDOWN_PIN) == GPIO.LOW:
                print("[HW] Shutdown button held — powering down")
                os.system("sudo shutdown now")
        time.sleep(0.1)

# ── CAMERA ──

def camera_capture(output_path: str) -> bool:
    """
    Trigger the D3400 shutter and save the image to output_path.
    Returns True on success.
    """
    if MOCK:
        # Generate a placeholder image with Pillow
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new('RGB', (1800, 1200), color=(30, 30, 35))
        draw = ImageDraw.Draw(img)
        # Draw a subtle grid and label
        for x in range(0, 1800, 100):
            draw.line([(x, 0), (x, 1200)], fill=(45, 45, 50), width=1)
        for y in range(0, 1200, 100):
            draw.line([(0, y), (1800, y)], fill=(45, 45, 50), width=1)
        # Centre label
        ts = datetime.now().strftime('%H:%M:%S')
        draw.text((900, 580), f"MOCK CAPTURE {ts}", fill=(120, 120, 130), anchor='mm')
        draw.text((900, 620), output_path.split('/')[-1], fill=(80, 80, 90), anchor='mm')
        img.save(output_path, 'JPEG', quality=92)
        print(f"[MOCK] Camera captured → {output_path}")
        return True

    if USE_WEBCAM:
        import subprocess
        result = subprocess.run(
            ['fswebcam', '--no-banner', '-r', '1280x720', output_path],
            capture_output=True
        )
        print(f"[WEBCAM] Captured → {output_path}")
        return result.returncode == 0

    try:
        camera = gp.Camera()
        camera.init()
        # Trigger flash
        flash_on()
        file_path = camera.capture(gp.GP_CAPTURE_IMAGE)
        flash_off()
        # Download to local path
        camera_file = camera.file_get(
            file_path.folder, file_path.name, gp.GP_FILE_TYPE_NORMAL
        )
        camera_file.save(output_path)
        camera.exit()
        print(f"[HW] Camera captured → {output_path}")
        return True
    except Exception as e:
        flash_off()
        print(f"[ERR] Camera capture failed: {e}")
        return False

# ── PRINTER ──

def printer_status() -> dict:
    """Returns { status: 'online'|'offline'|'busy', queue: int }"""
    if MOCK:
        return {'status': 'online', 'queue': 0}

    try:
        conn = cups.Connection()
        printers = conn.getPrinters()
        # Find the CP1500 (or first available printer)
        name = None
        for pname, pinfo in printers.items():
            if 'selphy' in pname.lower() or 'cp1500' in pname.lower():
                name = pname
                break
        if not name:
            name = list(printers.keys())[0] if printers else None
        if not name:
            return {'status': 'offline', 'queue': 0}

        jobs = conn.getJobs(which_jobs='not-completed')
        queue = sum(1 for j in jobs.values() if j.get('printer-uri', '').endswith(name))
        status = 'busy' if queue > 0 else 'online'
        return {'status': status, 'queue': queue}
    except Exception as e:
        print(f"[ERR] Printer status check failed: {e}")
        return {'status': 'offline', 'queue': 0}

def printer_print(file_path: str) -> bool:
    """Submit a print job. Returns True on success."""
    if MOCK:
        print(f"[MOCK] Print job submitted: {file_path}")
        # Simulate print time in background
        def _fake_print():
            indicator_set(True)
            time.sleep(3)
            indicator_set(False)
        threading.Thread(target=_fake_print, daemon=True).start()
        return True

    try:
        conn = cups.Connection()
        printers = conn.getPrinters()
        name = None
        for pname in printers:
            if 'selphy' in pname.lower() or 'cp1500' in pname.lower():
                name = pname
                break
        if not name:
            name = list(printers.keys())[0] if printers else None
        if not name:
            print("[ERR] No printer found")
            return False

        indicator_set(True)
        job_id = conn.printFile(name, file_path, "PhotoBooth Print", {})
        print(f"[HW] Print job {job_id} submitted to {name}")

        # Monitor job completion in background
        def _monitor():
            while True:
                time.sleep(1)
                jobs = conn.getJobs(which_jobs='not-completed')
                if job_id not in jobs:
                    indicator_set(False)
                    break
        threading.Thread(target=_monitor, daemon=True).start()
        return True
    except Exception as e:
        indicator_set(False)
        print(f"[ERR] Print failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  APP STATE
# ══════════════════════════════════════════════════════════════

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    # Default config
    return {
        "event_name": "PhotoBooth",
        "event_emoji": "📸",
        "active": True,
        "templates": {
            "single-classic": {
                "label": "Classic Frame",
                "shots": 1,
                "overlay": "overlays/overlay_classic.png",
                "composite": "build_single",
                "print_desc": "Full-frame 4×6 · dark border"
            },
            "single-gold": {
                "label": "Gold Corners",
                "shots": 1,
                "overlay": "overlays/overlay_goldcorners.png",
                "composite": "build_single",
                "print_desc": "Full-frame 4×6 · gold corner accents"
            },
            "strip-film": {
                "label": "Film Strip",
                "shots": 4,
                "overlay": "overlays/overlay_filmstrip.png",
                "composite": "build_strip",
                "print_desc": "4-shot strip · retro film surround"
            },
            "strip-party": {
                "label": "Party Grid",
                "shots": 4,
                "overlay": "overlays/overlay_partygrid.png",
                "composite": "build_strip",
                "print_desc": "4-shot 2×2 grid · festive overlay"
            }
        }
    }


config = load_config()

# Session state — only one active at a time
session = {
    'active':    False,
    'token':     None,
    'template':  None,
    'shot_num':  0,
    'shots_total': 0,
    'state':     'idle',      # idle | shooting | reviewing | printing
    'raw_files': [],          # paths to raw captures for this session
    'composite': None,        # path to final composited image
}

# Running totals — persisted to a small JSON file
stats_file = BASE_DIR / 'stats.json'

def load_stats():
    if stats_file.exists():
        with open(stats_file) as f:
            return json.load(f)
    return {'shots': 0, 'prints': 0, 'sessions': 0, 'paper': 108}

def save_stats():
    with open(stats_file, 'w') as f:
        json.dump(stats, f)

stats = load_stats()

# Photo registry — list of completed composites
# Each entry: { id, file, template, type, time }
photo_registry = []

def scan_existing_photos():
    """On startup, rebuild the registry from the photos/ directory."""
    for f in sorted(PHOTOS_DIR.glob('composite_*.jpg')):
        photo_registry.append({
            'id': len(photo_registry) + 1,
            'file': f.name,
            'template': 'Unknown',
            'type': 'single',
            'time': datetime.fromtimestamp(f.stat().st_mtime).strftime('%-I:%M %p'),
        })

# Event log (in-memory ring buffer)
event_log = []

def log_event(level: str, msg: str):
    ts = datetime.now().strftime('%H:%M:%S')
    event_log.append({'ts': ts, 'level': level, 'msg': msg})
    if len(event_log) > 100:
        event_log.pop(0)
    print(f"[{level.upper()}] {ts} {msg}")


# ══════════════════════════════════════════════════════════════
#  FLASK APP
# ══════════════════════════════════════════════════════════════

app = Flask(__name__, static_folder='static')


# ── Page routes ──

@app.route('/')
def page_booth():
    return send_from_directory(STATIC_DIR, 'booth.html')

@app.route('/admin')
def page_admin():
    return send_from_directory(TEMPLATES_DIR, 'admin.html')

@app.route('/gallery')
def page_gallery():
    return send_from_directory(TEMPLATES_DIR, 'gallery.html')

@app.route('/display')
def page_display():
    return send_from_directory(TEMPLATES_DIR, 'display.html')

# Serve photos directory
@app.route('/photos/<path:filename>')
def serve_photo(filename):
    return send_from_directory(PHOTOS_DIR, filename)

# Pull templates from json file
@app.route('/api/config')
def api_config():
    return jsonify({
        'event_name': config['event_name'],
        'templates': config['templates']
    })


# ── API: Status ──

@app.route('/api/status')
def api_status():
    ps = printer_status()
    return jsonify({
        'event':      config['event_name'],
        'boothUp':    True,
        'session':    session['state'],
        'template':   config['templates'].get(session['template'], {}).get('label') if session['template'] else None,
        'shotNum':    session['shot_num'],
        'shotsTotal': session['shots_total'],
        'printer':    ps['status'],
        'queue':      ps['queue'],
        'paper':      stats['paper'],
        'shots':      stats['shots'],
        'prints':     stats['prints'],
        'sessions':   stats['sessions'],
        'photos':     photo_registry[-8:],
        'log':        event_log[-30:],
    })


# ── API: Session lifecycle ──

@app.route('/api/session', methods=['POST'])
def api_session_start():
    """Guest hits 'I'm Ready' — lock the booth and start a session."""
    if session['active']:
        return jsonify({'error': 'Booth is busy'}), 409

    data = request.get_json(silent=True) or {}
    template_id = data.get('template', 'single-classic')

    if template_id not in config['templates']:
        return jsonify({'error': 'Unknown template'}), 400

    tpl = config['templates'][template_id]

    session['active']      = True
    session['token']       = str(uuid.uuid4())
    session['template']    = template_id
    session['shot_num']    = 0
    session['shots_total'] = tpl['shots']
    session['state']       = 'shooting'
    session['raw_files']   = []
    session['composite']   = None

    stats['sessions'] += 1
    save_stats()

    log_event('ok', f"Session started — {tpl['label']}")

    return jsonify({'token': session['token'], 'shots': tpl['shots']})


@app.route('/api/session/cancel', methods=['POST'])
def api_session_cancel():
    """Admin cancels the current session."""
    if session['active']:
        log_event('warn', f"Session cancelled (was: {session['state']})")
    _reset_session()
    return jsonify({'ok': True})


def _reset_session():
    session['active']    = False
    session['token']     = None
    session['template']  = None
    session['shot_num']  = 0
    session['shots_total'] = 0
    session['state']     = 'idle'
    session['raw_files'] = []
    session['composite'] = None


# ── API: Trigger (fire the shutter) ──

@app.route('/api/trigger', methods=['POST'])
def api_trigger():
    """Fire the camera shutter for the current session."""
    data = request.get_json(silent=True) or {}
    token = data.get('token')

    if not session['active'] or token != session['token']:
        return jsonify({'error': 'Invalid session'}), 403

    if session['state'] != 'shooting':
        return jsonify({'error': 'Not in shooting state'}), 409

    # Generate filename
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    shot_idx = session['shot_num']
    raw_name = f"raw_{ts}_{shot_idx}.jpg"
    raw_path = str(PHOTOS_DIR / raw_name)

    # Capture
    ok = camera_capture(raw_path)
    if not ok:
        return jsonify({'error': 'Capture failed'}), 500

    session['raw_files'].append(raw_path)
    session['shot_num'] = shot_idx + 1
    stats['shots'] += 1
    save_stats()

    log_event('ok', f"Shot {shot_idx + 1}/{session['shots_total']} captured")

    # If all shots done, composite and move to review
    if session['shot_num'] >= session['shots_total']:
        _do_composite()

    return jsonify({
        'ok': True,
        'shot': shot_idx + 1,
        'total': session['shots_total'],
        'done': session['shot_num'] >= session['shots_total'],
    })


def _do_composite():
    """Build the final composited image from raw captures."""
    tpl_id = session['template']
    tpl = config['templates'][tpl_id]

    overlay_path = str(BASE_DIR / tpl['overlay']) if tpl.get('overlay') else None

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_name = f"composite_{ts}.jpg"
    output_path = str(PHOTOS_DIR / output_name)

    # Choose compositing function
    if tpl['composite'] == 'build_strip':
        build_strip(session['raw_files'], overlay_path, output_path)
    else:
        build_single(session['raw_files'][0], overlay_path, output_path)

    session['composite'] = output_path
    session['state'] = 'reviewing'

    # Register in photo list
    type_map = {
        'single-classic': 'single', 'single-gold': 'single',
        'strip-film': 'strip', 'strip-party': 'grid',
    }
    photo_registry.append({
        'id':       len(photo_registry) + 1,
        'file':     output_name,
        'template': tpl['label'],
        'type':     type_map.get(tpl_id, 'single'),
        'time':     datetime.now().strftime('%-I:%M %p'),
    })

    log_event('ok', 'Composite built — guest reviewing')


# ── API: Latest (composited image for review screen) ──

@app.route('/api/latest')
def api_latest():
    if not session['composite']:
        return jsonify({'error': 'No composite available'}), 404
    filename = Path(session['composite']).name
    return jsonify({
        'url': f'/photos/{filename}',
        'file': filename,
    })


# ── API: Print ──

@app.route('/api/print', methods=['POST'])
def api_print():
    data = request.get_json(silent=True) or {}
    token = data.get('token')

    if not session['active'] or token != session['token']:
        return jsonify({'error': 'Invalid session'}), 403

    if not session['composite']:
        return jsonify({'error': 'Nothing to print'}), 400

    session['state'] = 'printing'
    log_event('ok', 'Print job submitted')

    ok = printer_print(session['composite'])

    if ok:
        stats['prints'] += 1
        stats['paper'] = max(0, stats['paper'] - 1)
        save_stats()

        if stats['paper'] <= 20 and stats['paper'] > 0:
            log_event('warn', f"Low paper: {stats['paper']} remaining")
        elif stats['paper'] == 0:
            log_event('err', 'Out of paper!')

        log_event('ok', f"Print complete — {stats['paper']} sheets remaining")

    _reset_session()
    return jsonify({'ok': ok})


# ── API: Download ──

@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.get_json(silent=True) or {}
    token = data.get('token')

    if not session['active'] or token != session['token']:
        return jsonify({'error': 'Invalid session'}), 403

    log_event('ok', 'Guest chose download only')
    _reset_session()
    return jsonify({'ok': True})


# ── API: Photos list ──

@app.route('/api/photos')
def api_photos():
    return jsonify({'photos': photo_registry})


# ── API: Printer status ──

@app.route('/api/printer-status')
def api_printer_status():
    ps = printer_status()
    ps['paper'] = stats['paper']
    return jsonify(ps)


# ── API: New cartridge ──

@app.route('/api/new-cartridge', methods=['POST'])
def api_new_cartridge():
    stats['paper'] = 108
    save_stats()
    log_event('ok', 'Paper counter reset to 108')
    return jsonify({'paper': 108})


# ── API: Shutdown ──

@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    log_event('err', 'Shutdown requested from admin')
    if MOCK:
        print("[MOCK] Shutdown requested — ignoring on laptop")
        return jsonify({'ok': True, 'mock': True})
    os.system("sudo shutdown now")
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════

def startup():
    gpio_setup()
    scan_existing_photos()

    # Shutdown button monitor
    t = threading.Thread(target=shutdown_monitor, daemon=True)
    t.start()

    mode = "MOCK" if MOCK else "HARDWARE"
    log_event('ok', f'PhotoBooth started ({mode} mode)')
    log_event('ok', f'Event: {config["event_name"]}')
    log_event('ok', f'Paper: {stats["paper"]}/108')
    log_event('ok', f'{len(photo_registry)} existing photos found')

    # Ready chime (Pi only)
    if not MOCK:
        chime = BASE_DIR / 'sounds' / 'ready.wav'
        if chime.exists():
            os.system(f"aplay {chime}")


if __name__ == '__main__':
    startup()
    app.run(host='0.0.0.0', port=5000, debug=MOCK)
