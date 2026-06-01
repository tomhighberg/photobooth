"""
PhotoBooth — app.py
Flask application serving the booth UI, admin dashboard, gallery,
projector display, and all API endpoints.

The camera is auto-detected at session start via gphoto2. Settings are
applied by matching the detected camera name against keys in camera_settings
in config.json. Set camera_model to "webcam" to use a USB webcam instead.

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

from flask import Flask, send_from_directory, jsonify, request, abort, render_template, redirect

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

try:
    from rpi_ws281x import PixelStrip, Color as LEDColor
    _WS281X_AVAILABLE = True
except ImportError:
    print("[WARN] rpi_ws281x not available — LED strip in console-log mode")
    _WS281X_AVAILABLE = False

# ── GPIO ──

FLASH_PIN     = 17
INDICATOR_PIN = 18
SHUTDOWN_PIN  = 27

# Button inputs (active LOW, internal pull-up)
TRIGGER_BTN_PIN = 5
PRINT_BTN_PIN   = 6
RESET_BTN_PIN   = 13

# Status output
READY_LIGHT_PIN  = 19

# Addressable LED strip (WS2812 / NeoPixel) — single data pin
LED_STRIP_PIN    = 12   # must be a hardware-PWM-capable pin (BCM 12 or 18)
LED_COUNT        = 8
LED_FREQ_HZ      = 800_000
LED_DMA          = 10
LED_BRIGHTNESS   = 200  # 0–255
LED_INVERT       = False
LED_CHANNEL      = 0    # 0 for pin 12/18, 1 for pin 13/19

_led_strip = None       # initialised in led_strip_init()

_PIN_NAMES = {
    FLASH_PIN: 'FLASH', INDICATOR_PIN: 'INDICATOR', SHUTDOWN_PIN: 'SHUTDOWN',
    TRIGGER_BTN_PIN: 'TRIGGER_BTN', PRINT_BTN_PIN: 'PRINT_BTN', RESET_BTN_PIN: 'RESET_BTN',
    READY_LIGHT_PIN: 'READY_LIGHT', LED_STRIP_PIN: 'LED_STRIP',
}

def _gpio_log(pin: int, state: str):
    print(f"[GPIO] pin {pin} ({_PIN_NAMES.get(pin, '?')}) → {state}")

def led_strip_init():
    global _led_strip
    if _WS281X_AVAILABLE:
        _led_strip = PixelStrip(
            LED_COUNT, LED_STRIP_PIN, LED_FREQ_HZ,
            LED_DMA, LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL,
        )
        _led_strip.begin()
        print(f"[LED] Strip initialised — {LED_COUNT} LEDs on pin {LED_STRIP_PIN}")
    else:
        print("[LED] Console-log mode")


def led_set_all(r: int, g: int, b: int):
    """Set all LEDs to a single RGB colour. Pass (0,0,0) to clear."""
    print(f"[LED] all → rgb({r},{g},{b})")
    if _WS281X_AVAILABLE and _led_strip:
        c = LEDColor(r, g, b)
        for i in range(LED_COUNT):
            _led_strip.setPixelColor(i, c)
        _led_strip.show()


def led_set_count(n: int, r: int, g: int, b: int):
    """Light the first n LEDs in colour (r,g,b), clear the rest."""
    print(f"[LED] {n}/{LED_COUNT} lit → rgb({r},{g},{b})")
    if _WS281X_AVAILABLE and _led_strip:
        on_c  = LEDColor(r, g, b)
        off_c = LEDColor(0, 0, 0)
        for i in range(LED_COUNT):
            _led_strip.setPixelColor(i, on_c if i < n else off_c)
        _led_strip.show()


def gpio_setup():
    if _GPIO_AVAILABLE:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(FLASH_PIN,        GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(INDICATOR_PIN,    GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(READY_LIGHT_PIN,  GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(SHUTDOWN_PIN,     GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(TRIGGER_BTN_PIN,  GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(PRINT_BTN_PIN,    GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(RESET_BTN_PIN,    GPIO.IN, pull_up_down=GPIO.PUD_UP)
        print("[GPIO] Setup complete")
    else:
        print("[GPIO] Console-log mode")
    led_strip_init()

def flash_on():
    _gpio_log(FLASH_PIN, 'ON')
    if _GPIO_AVAILABLE:
        GPIO.output(FLASH_PIN, GPIO.HIGH)

def flash_off():
    _gpio_log(FLASH_PIN, 'OFF')
    if _GPIO_AVAILABLE:
        GPIO.output(FLASH_PIN, GPIO.LOW)

def indicator_set(on: bool):
    _gpio_log(INDICATOR_PIN, 'ON' if on else 'OFF')
    if _GPIO_AVAILABLE:
        GPIO.output(INDICATOR_PIN, GPIO.HIGH if on else GPIO.LOW)

def _btn_pressed(pin: int) -> bool:
    """Return True if button is currently held LOW (debounced)."""
    if _GPIO_AVAILABLE:
        if GPIO.input(pin) == GPIO.LOW:
            time.sleep(0.05)
            return GPIO.input(pin) == GPIO.LOW
        return False
    return False


def _wait_for_button(pin: int, timeout: float = None) -> bool:
    """
    Block until pin goes LOW or timeout (seconds) elapses.
    Returns True if button was pressed, False on timeout.
    Also returns False immediately if RESET_BTN_PIN is pressed (caller handles reset).
    """
    start = time.time()
    while True:
        if _btn_pressed(RESET_BTN_PIN) and pin != RESET_BTN_PIN:
            return False
        if _btn_pressed(pin):
            # wait for release before returning
            while _GPIO_AVAILABLE and GPIO.input(pin) == GPIO.LOW:
                time.sleep(0.05)
            return True
        if timeout is not None and (time.time() - start) >= timeout:
            return False
        time.sleep(0.05)


def _ready_light(on: bool):
    _gpio_log(READY_LIGHT_PIN, 'ON' if on else 'OFF')
    if _GPIO_AVAILABLE:
        GPIO.output(READY_LIGHT_PIN, GPIO.HIGH if on else GPIO.LOW)


def _countdown_lights_clear():
    led_set_all(0, 0, 0)


# ── Countdown pattern — edit the colours/steps here ──────────────────────────
#
# Called once per shot. `seconds` is the countdown duration.
# Each step: (duration_secs, n_leds_lit, r, g, b)
# The strip drains from all-lit amber down to a single red LED, then
# flashes white at capture time (handled by the caller with flash_on/off).
#
_COUNTDOWN_STEPS = [
    (1.0, 8, 220, 120,   0),   # 3 — all 8 amber
    (1.0, 5, 220,  60,   0),   # 2 — 5 orange
    (1.0, 2, 200,   0,   0),   # 1 — 2 red
]

def _countdown(seconds: int = 3):
    """Run the LED strip countdown then clear. Capture happens after this returns."""
    steps = _COUNTDOWN_STEPS[:seconds]
    for duration, n, r, g, b in steps:
        led_set_count(n, r, g, b)
        time.sleep(duration)
    led_set_all(0, 0, 0)


def gpio_button_loop():
    """
    Background thread — drives a complete photo session from three physical
    buttons (trigger / print / reset) without touching the web UI.

    State machine:
      IDLE       → trigger pressed → start session
      COUNTDOWN  → 3-2-1 LEDs → capture shot → repeat for multi-shot templates
      REVIEWING  → print button to print, reset button to discard
      PRINTING   → waits for job, then returns to IDLE
    """
    log_event('ok', '[BTN] Button loop started')
    _ready_light(True)

    while True:
        # ── IDLE: wait for trigger ──────────────────────────────────────
        _ready_light(True)
        _countdown_lights_clear()

        pressed = _wait_for_button(TRIGGER_BTN_PIN)
        if not pressed:
            # reset was pressed in idle — nothing to cancel, just loop
            continue

        # ── SESSION START ───────────────────────────────────────────────
        if session['active']:
            log_event('warn', '[BTN] Trigger ignored — session already active')
            continue

        template_id = config.get('gpio_default_template') or next(iter(config['templates']))
        if template_id not in config['templates']:
            template_id = next(iter(config['templates']))

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
        _ready_light(False)
        log_event('ok', f"[BTN] Session started — {tpl['label']} ({tpl['shots']} shot(s))")

        # ── SHOOTING ────────────────────────────────────────────────────
        cancelled = False
        while session['shot_num'] < session['shots_total']:
            _countdown(3)

            if _btn_pressed(RESET_BTN_PIN):
                cancelled = True
                break

            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            shot_idx = session['shot_num']
            raw_path = str(PHOTOS_DIR / f"raw_{ts}_{shot_idx}.jpg")

            flash_on()
            ok = camera_capture(raw_path)
            flash_off()
            _countdown_lights_clear()

            if not ok:
                log_event('err', '[BTN] Capture failed — cancelling session')
                cancelled = True
                break

            session['raw_files'].append(raw_path)
            session['shot_num'] = shot_idx + 1
            stats['shots'] += 1
            save_stats()
            log_event('ok', f"[BTN] Shot {shot_idx + 1}/{session['shots_total']} captured")

            # Brief pause between shots so guest can reposition
            if session['shot_num'] < session['shots_total']:
                time.sleep(1.5)

        if cancelled:
            log_event('warn', '[BTN] Session cancelled during shooting')
            camera_disconnect()
            _reset_session()
            continue

        # ── COMPOSITE ───────────────────────────────────────────────────
        _do_composite()
        log_event('ok', '[BTN] Composite ready — waiting for print button')

        # Signal ready-to-print by blinking the ready light
        def _blink_ready():
            while session['state'] == 'reviewing':
                _ready_light(True);  time.sleep(0.4)
                _ready_light(False); time.sleep(0.4)
        blink_thread = threading.Thread(target=_blink_ready, daemon=True)
        blink_thread.start()

        # ── REVIEWING: wait for print or reset ─────────────────────────
        printed = False
        review_timeout = 60  # seconds before auto-reset
        start = time.time()
        while time.time() - start < review_timeout:
            if _btn_pressed(RESET_BTN_PIN):
                log_event('warn', '[BTN] Print discarded by reset button')
                break
            if _btn_pressed(PRINT_BTN_PIN):
                if not session['composite_valid']:
                    log_event('err', '[BTN] Print blocked — composite failed size check')
                    # Keep waiting; guest or operator can reset manually
                    time.sleep(1)
                    continue
                printed = True
                break
            time.sleep(0.05)

        _ready_light(False)

        if printed:
            session['state'] = 'printing'
            log_event('ok', '[BTN] Print button pressed')
            ok = printer_print(session['composite'])
            if ok:
                stats['prints'] += 1
                stats['paper'] = max(0, stats['paper'] - 1)
                save_stats()
                if stats['paper'] <= 20:
                    log_event('warn', f"[BTN] Low paper: {stats['paper']} remaining")
                log_event('ok', f"[BTN] Print submitted — {stats['paper']} sheets remaining")
            else:
                log_event('err', '[BTN] Print failed')

        camera_disconnect()
        _reset_session()


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
# CAMERA_MODEL starts as 'auto' and is resolved at connect time via gphoto2
# auto-detection. Set camera_model to "webcam" in config.json to bypass this.

_camera = None      # persistent gphoto2 handle; shared across shots in a session
CAMERA_MODEL = 'auto'  # overwritten by config load; updated again at connect time


def _detect_camera() -> tuple | None:
    """
    Query gphoto2 for attached cameras.
    Returns (model_name, port) for the first detected camera, or None.
    """
    try:
        cameras = list(gp.Camera.autodetect())
        if not cameras:
            print("[CAM] No cameras detected via gphoto2")
            return None
        name, port = cameras[0]
        print(f"[CAM] Detected: {name} on {port}")
        return name, port
    except Exception as e:
        print(f"[CAM] Auto-detect error: {e}")
        return None


def _resolve_settings(detected_name: str) -> tuple:
    """
    Match gphoto2-reported camera name against camera_settings keys in config.
    Tries exact match, then case-insensitive, then partial substring.
    Returns (matched_key, settings_dict).
    """
    cam_settings = config.get('camera_settings', {})
    detected_lower = detected_name.lower()

    for key in cam_settings:
        if key.lower() == detected_lower:
            return key, cam_settings[key]

    for key in cam_settings:
        key_lower = key.lower()
        if key_lower in detected_lower or detected_lower in key_lower:
            return key, cam_settings[key]

    print(f"[CAM] No settings profile matched '{detected_name}' — proceeding without settings")
    return detected_name, {}


def camera_connect() -> bool:
    """Detect, open, and configure the camera. Returns True on success."""
    global _camera, CAMERA_MODEL

    if CAMERA_MODEL == 'webcam':
        return True

    if not _GP_AVAILABLE:
        print("[ERR] gphoto2 not available")
        return False

    detected = _detect_camera()
    if detected is None:
        return False

    detected_name, _ = detected
    resolved_key, cam_cfg = _resolve_settings(detected_name)
    CAMERA_MODEL = resolved_key

    try:
        _camera = gp.Camera()
        _camera.init()
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
    """Release the gphoto2 camera handle."""
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
    return _capture_gphoto2(output_path)


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
        file_path = _camera.capture(gp.GP_CAPTURE_IMAGE)
        print(f"[CAM] Camera file: {file_path.folder}/{file_path.name}")
        camera_file = _camera.file_get(
            file_path.folder, file_path.name, gp.GP_FILE_TYPE_NORMAL
        )
        camera_file.save(output_path)
        print(f"[CAM] {CAMERA_MODEL} captured → {output_path}")
        return True
    except Exception as e:
        print(f"[ERR] {CAMERA_MODEL} capture failed: {e}")
        camera_disconnect()
        return False

# ── PRINTER ──

def printer_status() -> dict:
    """Returns { status: 'online'|'offline'|'busy', queue: int, name: str }"""
    if MOCK_PRINTER:
        return {'status': 'online', 'queue': 0, 'name': 'CP1500'}

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
            return {'status': 'offline', 'queue': 0, 'name': 'No printer'}

        jobs = conn.getJobs(which_jobs='not-completed')
        queue = sum(1 for j in jobs.values() if j.get('printer-uri', '').endswith(name))
        status = 'busy' if queue > 0 else 'online'
        return {'status': status, 'queue': queue, 'name': name}
    except Exception as e:
        print(f"[ERR] Printer status check failed: {e}")
        return {'status': 'offline', 'queue': 0, 'name': 'No printer'}

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


# Composites below this size are treated as blank/failed (overlay-only, no photo).
# A real photo composite is typically 500 KB+; a blank is ~30–80 KB.
COMPOSITE_MIN_BYTES = 150_000

config = load_config()

CAMERA_MODEL = config.get('camera_model', 'auto')
# 'webcam' is the only value treated specially; anything else triggers auto-detection
print(f"[CAM] Camera mode: {CAMERA_MODEL}")

# Session state — only one active at a time
session = {
    'active':          False,
    'token':           None,
    'template':        None,
    'shot_num':        0,
    'shots_total':     0,
    'state':           'idle',   # idle | shooting | reviewing | printing
    'raw_files':       [],       # paths to raw captures for this session
    'composite':       None,     # path to final composited image
    'composite_valid': False,    # False if composite looks blank (file too small)
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
    return send_from_directory(STATIC_DIR, 'display.html')

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
        'templates': config['templates'],
        'gpio_default_template': config.get('gpio_default_template', ''),
    })


@app.route('/api/config/gpio-template', methods=['POST'])
def api_set_gpio_template():
    """Admin sets which template the button loop uses by default."""
    data = request.get_json(silent=True) or {}
    template_id = data.get('template')
    if template_id not in config['templates']:
        return jsonify({'error': 'Unknown template'}), 400
    config['gpio_default_template'] = template_id
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)
    log_event('ok', f"GPIO default template → {config['templates'][template_id]['label']}")
    return jsonify({'ok': True, 'template': template_id})


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
        'printerName': ps['name'],
        'queue':      ps['queue'],
        'paper':      stats['paper'],
        'shots':      stats['shots'],
        'prints':     stats['prints'],
        'sessions':   stats['sessions'],
        'photos':              photo_registry[-8:],
        'log':                 event_log[-30:],
        'gpioTemplate':        config.get('gpio_default_template', ''),
        'compositeValid':      session['composite_valid'],
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
    _ready_light(False)
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
    session['active']          = False
    session['token']           = None
    session['template']        = None
    session['shot_num']        = 0
    session['shots_total']     = 0
    session['state']           = 'idle'
    session['raw_files']       = []
    session['composite']       = None
    session['composite_valid'] = False
    _ready_light(True)


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
    flash_on()
    ok = camera_capture(raw_path)
    flash_off()
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

    # Validate composite — reject if suspiciously small (likely blank/no photo fired)
    actual_bytes = Path(output_path).stat().st_size
    if actual_bytes < COMPOSITE_MIN_BYTES:
        session['composite_valid'] = False
        log_event('err', f'Composite looks blank ({actual_bytes // 1024} KB < {COMPOSITE_MIN_BYTES // 1024} KB threshold) — print blocked')
    else:
        session['composite_valid'] = True
        log_event('ok', f'Composite built ({actual_bytes // 1024} KB) — guest reviewing')

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


# ── API: Delete photo ──

@app.route('/api/photos/<int:photo_id>', methods=['DELETE'])
def api_delete_photo(photo_id):
    global photo_registry
    entry = next((p for p in photo_registry if p['id'] == photo_id), None)
    if not entry:
        return jsonify({'error': 'Not found'}), 404
    file_path = PHOTOS_DIR / entry['file']
    if file_path.exists():
        file_path.unlink()
    photo_registry[:] = [p for p in photo_registry if p['id'] != photo_id]
    log_event('warn', f"Photo deleted: {entry['file']}")
    return jsonify({'ok': True})


# ── API: Raw shots list ──

@app.route('/api/photos/raw')
def api_raw_photos():
    raw_files = sorted(PHOTOS_DIR.glob('raw_*.jpg'), key=lambda f: f.stat().st_mtime)
    return jsonify({'photos': [
        {
            'file': f.name,
            'time': datetime.fromtimestamp(f.stat().st_mtime).strftime('%-I:%M %p'),
            'size': round(f.stat().st_size / 1024),
        }
        for f in raw_files
    ]})


# ── API: Delete raw shot ──

@app.route('/api/photos/raw/<filename>', methods=['DELETE'])
def api_delete_raw(filename):
    if not filename.startswith('raw_') or not filename.endswith('.jpg') or '/' in filename or '..' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    file_path = PHOTOS_DIR / filename
    if not file_path.exists():
        return jsonify({'error': 'Not found'}), 404
    file_path.unlink()
    log_event('warn', f"Raw shot deleted: {filename}")
    return jsonify({'ok': True})


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
    if not _GPIO_AVAILABLE:
        print("[GPIO] Shutdown requested — ignoring (no GPIO)")
        return jsonify({'ok': True, 'gpio': False})
    os.system("sudo shutdown now")
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════

def startup():
    gpio_setup()
    scan_existing_photos()

    t = threading.Thread(target=shutdown_monitor, daemon=True)
    t.start()

    b = threading.Thread(target=gpio_button_loop, daemon=True)
    b.start()

    log_event('ok', f'PhotoBooth started — camera: {CAMERA_MODEL}')
    log_event('ok', f'Event: {config["event_name"]}')
    log_event('ok', f'Paper: {stats["paper"]}/108')
    log_event('ok', f'{len(photo_registry)} existing photos found')

    # Ready chime (Pi only)
    if _GPIO_AVAILABLE:
        chime = BASE_DIR / 'sounds' / 'ready.wav'
        if chime.exists():
            os.system(f"aplay {chime}")


if __name__ == '__main__':
    startup()
    app.run(host='0.0.0.0', port=5000, debug=False)
