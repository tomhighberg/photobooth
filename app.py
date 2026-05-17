"""
PhotoBooth — app.py
Flask application serving the booth UI, admin dashboard, gallery,
projector display, and all API endpoints.

Camera model is selected via camera_model in config.json:
  "webcam"     — USB webcam via fswebcam (for testing)
  "d3400"      — Nikon D3400 via gphoto2
  "lumix_s5ii" — Panasonic Lumix S5 II via gphoto2

Set MOCK_PRINTER = True to log print jobs to console instead of sending
to the physical printer (useful when the printer isn't connected).
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

MOCK_PRINTER = False  # Set True to log print jobs without using the physical printer

BASE_DIR   = Path(__file__).parent
PHOTOS_DIR = BASE_DIR / 'photos'
OVERLAYS_DIR = BASE_DIR / 'overlays'
STATIC_DIR = BASE_DIR / 'static'
TEMPLATES_DIR = BASE_DIR / 'templates'
CONFIG_FILE = BASE_DIR / 'config.json'

PHOTOS_DIR.mkdir(exist_ok=True)
OVERLAYS_DIR.mkdir(exist_ok=True)

# ══════════════════════════════════════════════════════════════
#  HARDWARE IMPORTS
#  Each library is imported independently so a missing library
#  only disables that subsystem, not the whole app.
# ══════════════════════════════════════════════════════════════

try:
    import gphoto2 as gp
    _GP_AVAILABLE = True
except ImportError:
    print("[WARN] gphoto2 not available — gphoto2 cameras disabled")
    _GP_AVAILABLE = False

try:
    import cups
    _CUPS_AVAILABLE = True
except ImportError:
    print("[WARN] cups not available — physical printing disabled")
    _CUPS_AVAILABLE = False

try:
    import RPi.GPIO as GPIO
    _GPIO_AVAILABLE = True
except ImportError:
    print("[WARN] RPi.GPIO not available — GPIO in console-log mode")
    _GPIO_AVAILABLE = False

# ── GPIO ──

FLASH_PIN     = 17
INDICATOR_PIN = 18
SHUTDOWN_PIN  = 27

_PIN_NAMES = {FLASH_PIN: 'FLASH', INDICATOR_PIN: 'INDICATOR', SHUTDOWN_PIN: 'SHUTDOWN'}

def _gpio_log(pin: int, state: str):
    print(f"[GPIO] pin {pin} ({_PIN_NAMES.get(pin, '?')}) → {state}")

def gpio_setup():
    if _GPIO_AVAILABLE:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(FLASH_PIN, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(INDICATOR_PIN, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(SHUTDOWN_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        print("[GPIO] Setup complete — pins 17 (FLASH), 18 (INDICATOR), 27 (SHUTDOWN)")
    else:
        print("[GPIO] Console-log mode — pins 17 (FLASH), 18 (INDICATOR), 27 (SHUTDOWN)")

def flash_on():
    if _GPIO_AVAILABLE:
        GPIO.output(FLASH_PIN, GPIO.HIGH)
    else:
        _gpio_log(FLASH_PIN, 'ON')

def flash_off():
    if _GPIO_AVAILABLE:
        GPIO.output(FLASH_PIN, GPIO.LOW)
    else:
        _gpio_log(FLASH_PIN, 'OFF')

def indicator_set(on: bool):
    if _GPIO_AVAILABLE:
        GPIO.output(INDICATOR_PIN, GPIO.HIGH if on else GPIO.LOW)
    else:
        _gpio_log(INDICATOR_PIN, 'ON' if on else 'OFF')

def shutdown_monitor():
    """Background thread — polls shutdown button, triggers graceful shutdown."""
    while True:
        if _GPIO_AVAILABLE:
            if GPIO.input(SHUTDOWN_PIN) == GPIO.LOW:
                time.sleep(3)
                if GPIO.input(SHUTDOWN_PIN) == GPIO.LOW:
                    print("[GPIO] Shutdown button held — powering down")
                    os.system("sudo shutdown now")
            time.sleep(0.1)
        else:
            time.sleep(5)

# ── CAMERA ──
# CAMERA_MODEL is set after config is loaded (see below).
# Supported values: "webcam" | "d3400" | "lumix_s5ii"

_camera = None  # persistent gphoto2 handle; shared across shots in a session


def camera_connect() -> bool:
    """Open and configure the camera. No-op for webcam. Returns True on success."""
    global _camera
    if CAMERA_MODEL == 'webcam':
        return True
    if not _GP_AVAILABLE:
        print(f"[ERR] gphoto2 not available — cannot connect {CAMERA_MODEL}")
        return False
    try:
        _camera = gp.Camera()
        _camera.init()
        cam_cfg = config.get('camera_settings', {}).get(CAMERA_MODEL, {})
        if cam_cfg:
            _camera_configure(cam_cfg)
        print(f"[CAM] {CAMERA_MODEL} connected")
        return True
    except Exception as e:
        print(f"[ERR] Camera connect failed: {e}")
        _camera = None
        return False


def _camera_configure(cfg_dict: dict):
    """Apply gphoto2 config key/value pairs from config.json."""
    try:
        cfg = _camera.get_config()
        for key, value in cfg_dict.items():
            try:
                widget = cfg.get_child_by_name(key)
                widget.set_value(value)
            except Exception as e:
                print(f"[WARN] Camera config {key}={value} not applied: {e}")
        _camera.set_config(cfg)
        print(f"[CAM] Configuration applied: {cfg_dict}")
    except Exception as e:
        print(f"[WARN] Camera configure failed: {e}")


def camera_disconnect():
    """Release the gphoto2 camera handle. No-op for webcam."""
    global _camera
    if _camera is not None:
        try:
            _camera.exit()
            print(f"[CAM] {CAMERA_MODEL} disconnected")
        except Exception:
            pass
        _camera = None


def camera_capture(output_path: str) -> bool:
    """Capture one image and save to output_path. Returns True on success."""
    if CAMERA_MODEL == 'webcam':
        return _capture_webcam(output_path)
    elif CAMERA_MODEL in ('d3400', 'lumix_s5ii'):
        return _capture_gphoto2(output_path)
    else:
        print(f"[ERR] Unknown camera model: {CAMERA_MODEL}")
        return False


def _capture_webcam(output_path: str) -> bool:
    import subprocess
    result = subprocess.run(
        ['fswebcam', '--no-banner', '-r', '1280x720', output_path],
        capture_output=True
    )
    print(f"[WEBCAM] Captured → {output_path}")
    return result.returncode == 0


def _capture_gphoto2(output_path: str) -> bool:
    global _camera
    if _camera is None:
        if not camera_connect():
            return False
    try:
        flash_on()
        file_path = _camera.capture(gp.GP_CAPTURE_IMAGE)
        flash_off()
        camera_file = _camera.file_get(
            file_path.folder, file_path.name, gp.GP_FILE_TYPE_NORMAL
        )
        camera_file.save(output_path)
        print(f"[CAM] {CAMERA_MODEL} captured → {output_path}")
        return True
    except Exception as e:
        flash_off()
        print(f"[ERR] {CAMERA_MODEL} capture failed: {e}")
        camera_disconnect()  # force reconnect on next shot
        return False

# ── PRINTER ──

def printer_status() -> dict:
    """Returns { status: 'online'|'offline'|'busy', queue: int }"""
    if MOCK_PRINTER:
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
    if MOCK_PRINTER:
        print(f"[PRINTER] Mock print job: {file_path}")
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

CAMERA_MODEL = config.get('camera_model', 'webcam')
print(f"[CAM] Using camera model: {CAMERA_MODEL}")

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

# --- Captive Portal ---
@app.route('/hotspot-detect.html')
@app.route('/generate_204')
@app.route('/connecttest.txt')
def captive_portal():
    return redirect('http://192.168.4.1:5000')

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

# Serve overlays directory
@app.route('/overlays/<path:filename>')
def serve_overlay(filename):
    return send_from_directory(OVERLAYS_DIR, filename)

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

    camera_connect()
    log_event('ok', f"Session started — {tpl['label']}")

    return jsonify({'token': session['token'], 'shots': tpl['shots']})


@app.route('/api/session/cancel', methods=['POST'])
def api_session_cancel():
    """Admin cancels the current session."""
    if session['active']:
        log_event('warn', f"Session cancelled (was: {session['state']})")
    camera_disconnect()
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

    camera_disconnect()
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
    camera_disconnect()
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
