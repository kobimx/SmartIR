#!/usr/bin/env python3
"""
SmartIR IR Code Capture Tool
Captures IR codes via zigbee2mqtt (iH-F8260) and builds SmartIR-compatible JSON files.

Requires: paho-mqtt  (pip install paho-mqtt)
"""

import json
import os
import threading
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    import paho.mqtt.client as mqtt
    PAHO_AVAILABLE = True
except ImportError:
    PAHO_AVAILABLE = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
TOOLS_DIR = Path(__file__).parent
SESSION_FILE = TOOLS_DIR / "capture_session.json"
CODES_DIR = TOOLS_DIR.parent / "codes" / "climate"

# Sentinel value stored in session for skipped combos
_SKIPPED = "__skipped__"


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def gen_temps(min_t: float, max_t: float, precision: float) -> list:
    """Generate temperature key strings matching SmartIR JSON format."""
    temps = []
    t = min_t
    while round(t, 6) <= round(max_t, 6):
        if precision == int(precision):
            temps.append(str(int(round(t))))
        else:
            temps.append(str(round(t, 1)))
        t = round(t + precision, 6)
    return temps


def build_combos(config: dict) -> list:
    """Return ordered list of combo dicts: first 'off', then all mode/fan/[swing]/temp."""
    single_code = set(config.get("singleCodeModes", []))
    excluded    = set(config.get("excludedModes", []))
    combos = [{"type": "off"}]
    for mode in config["operationModes"]:
        if mode in excluded:
            continue
        is_single = mode in single_code
        for fan in config["fanModes"]:
            temps = gen_temps(config["minTemperature"], config["maxTemperature"], config["precision"])
            capture_temps = [temps[0]] if is_single else temps
            if config.get("swingModes"):
                for swing in config["swingModes"]:
                    for temp in capture_temps:
                        combos.append({"type": "code", "mode": mode, "fan": fan,
                                       "swing": swing, "temp": temp, "single_code": is_single})
            else:
                for temp in capture_temps:
                    combos.append({"type": "code", "mode": mode, "fan": fan,
                                   "temp": temp, "single_code": is_single})
    return combos


def combo_key(combo: dict) -> str:
    """Unique string key for a combo (used as dict key in session codes)."""
    if combo["type"] == "off":
        return "off"
    parts = [combo["mode"], combo["fan"]]
    if "swing" in combo:
        parts.append(combo["swing"])
    parts.append(combo["temp"])
    return "|".join(parts)


def combo_label(combo: dict) -> str:
    """Human-readable label for display in the UI."""
    if combo["type"] == "off":
        return "OFF"
    parts = [combo["mode"].upper(), combo["fan"]]
    if "swing" in combo:
        parts.append(combo["swing"])
    if combo.get("single_code"):
        parts.append("all temps")
    else:
        parts.append(f"{combo['temp']}°C")
    return " / ".join(parts)


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class SmartIRCapture(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("SmartIR IR Code Capture")
        self.geometry("960x700")
        self.minsize(820, 600)

        # Runtime state
        self.session: dict = {}
        self.combos: list = []
        self.current_idx: int = 0
        self.pending_code: str | None = None
        self.listening: bool = False
        self._listen_timer = None
        self.mqtt_client = None
        self._friendly_name: str = ""

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not PAHO_AVAILABLE:
            messagebox.showerror(
                "Missing dependency",
                "paho-mqtt is not installed.\n\nRun:  pip install paho-mqtt"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # UI Construction
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self.tab_setup    = ttk.Frame(self.notebook, padding=10)
        self.tab_capture  = ttk.Frame(self.notebook, padding=10)
        self.tab_overview = ttk.Frame(self.notebook, padding=10)
        self.tab_export   = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.tab_setup,    text="⚙  Setup")
        self.notebook.add(self.tab_capture,  text="📡  Capture")
        self.notebook.add(self.tab_overview, text="📊  Overview")
        self.notebook.add(self.tab_export,   text="💾  Export")

        self._build_setup_tab()
        self._build_capture_tab()
        self._build_overview_tab()
        self._build_export_tab()

        # Capture / Overview / Export locked until MQTT connects
        self.notebook.tab(1, state="disabled")
        self.notebook.tab(2, state="disabled")
        self.notebook.tab(3, state="disabled")

    # ── Setup tab ─────────────────────────────────────────────────────────────

    def _build_setup_tab(self):
        tab = self.tab_setup
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)

        self._sv: dict[str, tk.StringVar] = {}

        def labeled_entry(parent, row, label, key, default="", show=None):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
            sv = tk.StringVar(value=default)
            self._sv[key] = sv
            kw = {"textvariable": sv, "width": 28}
            if show:
                kw["show"] = show
            e = ttk.Entry(parent, **kw)
            e.grid(row=row, column=1, sticky="ew", padx=(6, 0))
            parent.columnconfigure(1, weight=1)

        # ── MQTT ──
        mqtt_frame = ttk.LabelFrame(tab, text="MQTT Broker", padding=8)
        mqtt_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))

        labeled_entry(mqtt_frame, 0, "Host:",                    "mqtt_host",   "homeassistant.local")
        labeled_entry(mqtt_frame, 1, "Port:",                    "mqtt_port",   "1883")
        labeled_entry(mqtt_frame, 2, "Username (optional):",     "mqtt_user",   "")
        labeled_entry(mqtt_frame, 3, "Password (optional):",     "mqtt_pass",   "", show="•")
        labeled_entry(mqtt_frame, 4, "z2m device friendly name:", "device_name", "ir_remote")

        # ── AC info ──
        ac_frame = ttk.LabelFrame(tab, text="Air Conditioner Info", padding=8)
        ac_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 6))

        labeled_entry(ac_frame, 0, "Manufacturer:",              "manufacturer", "Mitsubishi Electric")
        labeled_entry(ac_frame, 1, "Model(s) (comma-sep):",      "models",       "MSZ-WN35VA")
        labeled_entry(ac_frame, 2, "Min Temperature:",           "min_temp",     "16")
        labeled_entry(ac_frame, 3, "Max Temperature:",           "max_temp",     "30")
        labeled_entry(ac_frame, 4, "Precision (1 or 0.5):",      "precision",    "1")

        # ── Modes ──
        modes_frame = ttk.LabelFrame(tab, text="Modes", padding=8)
        modes_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(0, 6))
        modes_frame.columnconfigure(1, weight=1)

        _MODE_HELP = {
            "op_modes": (
                "Operation Modes",
                "Must match Home Assistant HVACMode values (invalid entries are silently dropped).\n\n"
                "Accepted values:\n"
                "  cool       — Cooling\n"
                "  heat       — Heating\n"
                "  dry        — Dehumidify\n"
                "  fan_only   — Fan only, no cooling/heating\n"
                "  auto       — Automatic / AI scheduling\n"
                "  heat_cool  — Heat+cool (dual setpoint)\n\n"
                "Note: 'off' is added automatically — do not include it here.\n"
                "Custom strings (e.g. 'ifeel', 'cool_powerful') are stored in the JSON\n"
                "but will be ignored by HA's climate entity unless HA recognises them."
            ),
            "fan_modes": (
                "Fan Modes",
                "Free-form strings — use any names your remote supports.\n"
                "SmartIR passes them through as-is to Home Assistant.\n\n"
                "Common values used in existing SmartIR codes:\n"
                "  auto       — Automatic fan speed\n"
                "  quiet      — Quiet / silent\n"
                "  low        — Low speed\n"
                "  mid  / med — Medium speed\n"
                "  high       — High speed\n"
                "  highest    — Max speed (some devices)\n"
                "  turbo      — Turbo / boost\n"
                "  super high — Labelled as-is on some Mitsubishi models\n\n"
                "Capitalisation matters — use exactly the string you want\n"
                "shown in the HA climate card."
            ),
            "swing_modes": (
                "Swing Modes",
                "Free-form strings — optional. Leave empty if your remote has no swing.\n"
                "SmartIR passes them through as-is to Home Assistant.\n\n"
                "Common values used in existing SmartIR codes:\n"
                "  auto                  — Automatic swing\n"
                "  swing                 — Continuous swing\n"
                "  horizontal            — Horizontal vane only\n"
                "  vertical              — Vertical vane only\n"
                "  both                  — Both axes\n"
                "  position 1 … 5        — Fixed vane positions (Mitsubishi style)\n"
                "  Top / High / Mid / Low / Bottom  — Named positions\n\n"
                "Tip: name them to match what the HA climate card should display."
            ),
            "single_code_modes": (
                "Single-code Modes",
                "Modes where temperature does NOT affect the IR signal.\n"
                "One code is captured per fan/swing combination and applied to ALL temperatures.\n\n"
                "Typical values:\n"
                "  dry        — Dehumidify (AC ignores the setpoint temperature)\n"
                "  fan_only   — Fan only (no heating/cooling, temperature is irrelevant)\n\n"
                "Leave empty to capture the full temperature range for every mode."
            ),
            "excluded_modes": (
                "Excluded Modes",
                "Modes to skip entirely — no codes will be captured or exported.\n\n"
                "Useful when your unit does not support a mode at all:\n"
                "  auto       — Automatic / AI mode (not on all units)\n"
                "  heat_cool  — Dual-setpoint mode (rare)\n\n"
                "Excluded modes are removed from the exported JSON operationModes list\n"
                "so Home Assistant will not show them in the climate card."
            ),
        }

        def _show_help(key):
            title, body = _MODE_HELP[key]
            win = tk.Toplevel(self)
            win.title(title)
            win.resizable(False, False)
            win.grab_set()
            ttk.Label(win, text=title, font=("TkDefaultFont", 11, "bold")).pack(
                padx=16, pady=(14, 4), anchor="w")
            ttk.Separator(win).pack(fill=tk.X, padx=16)
            ttk.Label(win, text=body, justify="left", font=("Courier", 9)).pack(
                padx=16, pady=(8, 4), anchor="w")
            ttk.Button(win, text="Close", command=win.destroy).pack(pady=(4, 14))
            win.update_idletasks()
            # Centre over parent
            px, py = self.winfo_x(), self.winfo_y()
            pw, ph = self.winfo_width(), self.winfo_height()
            ww, wh = win.winfo_width(), win.winfo_height()
            win.geometry(f"+{px + (pw - ww) // 2}+{py + (ph - wh) // 2}")

        def modes_row(parent, row, label, key, default):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
            sv = tk.StringVar(value=default)
            self._sv[key] = sv
            ttk.Entry(parent, textvariable=sv).grid(row=row, column=1, sticky="ew", padx=(6, 0))
            btn = ttk.Button(parent, text="ⓘ", width=3,
                             command=lambda k=key: _show_help(k))
            btn.grid(row=row, column=2, padx=(4, 0))

        modes_row(modes_frame, 0, "Operation modes:",   "op_modes",          "cool, heat, dry, fan_only")
        modes_row(modes_frame, 1, "Fan modes:",         "fan_modes",         "auto, low, mid, high")
        modes_row(modes_frame, 2, "Swing modes:",       "swing_modes",       "")
        modes_row(modes_frame, 3, "Single-code modes:", "single_code_modes", "dry, fan_only")
        modes_row(modes_frame, 4, "Excluded modes:",    "excluded_modes",    "")

        # ── Actions ──
        btn_frame = ttk.Frame(tab)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        ttk.Button(btn_frame, text="📂  Load Session",          command=self._load_session).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="🔌  Connect & Start Capture", command=self._start_capture).pack(side=tk.LEFT)

        self._setup_status_var = tk.StringVar()
        ttk.Label(tab, textvariable=self._setup_status_var, foreground="steelblue").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

    # ── Capture tab ───────────────────────────────────────────────────────────

    def _build_capture_tab(self):
        tab = self.tab_capture
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        # Progress
        prog_frame = ttk.Frame(tab)
        prog_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        prog_frame.columnconfigure(1, weight=1)

        self._progress_label_var = tk.StringVar(value="0 / 0")
        ttk.Label(prog_frame, textvariable=self._progress_label_var, width=14,
                  font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, sticky="w")
        self._progress_bar = ttk.Progressbar(prog_frame, mode="determinate")
        self._progress_bar.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        # Current target
        target_frame = ttk.LabelFrame(tab, text="Current Target", padding=10)
        target_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        target_frame.columnconfigure(0, weight=1)

        self._combo_label_var = tk.StringVar(value="—")
        ttk.Label(target_frame, textvariable=self._combo_label_var,
                  font=("TkDefaultFont", 20, "bold"), anchor="center").grid(sticky="ew")

        # Labeled breakdown — explains what each segment of the target means
        self._breakdown_frame = ttk.Frame(target_frame)
        self._breakdown_frame.grid(sticky="ew", pady=(6, 0))
        for col in range(8):
            self._breakdown_frame.columnconfigure(col, weight=1)

        # Pairs: (heading_var, value_var) per field — built dynamically in _update_capture_ui
        self._breakdown_cells: list[tuple[tk.StringVar, tk.StringVar]] = []
        for i in range(4):  # max 4 fields: mode / fan / swing / temp
            hv = tk.StringVar()
            vv = tk.StringVar()
            ttk.Label(self._breakdown_frame, textvariable=hv,
                      foreground="gray", font=("TkDefaultFont", 8)).grid(
                row=0, column=i * 2, padx=(8, 0), sticky="w")
            ttk.Label(self._breakdown_frame, textvariable=vv,
                      font=("TkDefaultFont", 9, "bold")).grid(
                row=1, column=i * 2, padx=(8, 0), sticky="w")
            if i < 3:
                ttk.Label(self._breakdown_frame, text="│", foreground="#cccccc").grid(
                    row=0, column=i * 2 + 1, rowspan=2, padx=4)
            self._breakdown_cells.append((hv, vv))

        self._status_var = tk.StringVar(value="Press ▶ Capture, then press the remote button.")
        self._status_label = ttk.Label(target_frame, textvariable=self._status_var, anchor="center")
        self._status_label.grid(sticky="ew", pady=(6, 0))

        # Code preview
        preview_frame = ttk.LabelFrame(tab, text="Received Code", padding=5)
        preview_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
        preview_frame.rowconfigure(0, weight=1)
        preview_frame.columnconfigure(0, weight=1)

        self._code_preview = tk.Text(preview_frame, height=5, wrap=tk.WORD,
                                     state="disabled", background="#f0f0f0",
                                     font=("Courier", 9))
        self._code_preview.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(preview_frame, command=self._code_preview.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._code_preview.configure(yscrollcommand=sb.set)

        # Action buttons
        btn_frame = ttk.Frame(tab)
        btn_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))

        self._btn_capture = ttk.Button(btn_frame, text="▶  Capture",
                                       command=self._do_capture, width=14)
        self._btn_capture.pack(side=tk.LEFT, padx=(0, 4))

        self._btn_confirm = ttk.Button(btn_frame, text="✓  Confirm",
                                       command=self._do_confirm, width=14, state="disabled")
        self._btn_confirm.pack(side=tk.LEFT, padx=(0, 4))

        self._btn_retry = ttk.Button(btn_frame, text="↩  Retry",
                                     command=self._do_retry, width=10, state="disabled")
        self._btn_retry.pack(side=tk.LEFT, padx=(0, 4))

        self._btn_copy = ttk.Button(btn_frame, text="⤵  Copy to remaining temps",
                                    command=self._do_copy_remaining, state="disabled")
        self._btn_copy.pack(side=tk.LEFT, padx=(0, 4))

        self._btn_skip = ttk.Button(btn_frame, text="⏭  Skip",
                                    command=self._do_skip, width=8)
        self._btn_skip.pack(side=tk.LEFT, padx=(0, 4))

        self._btn_back = ttk.Button(btn_frame, text="← Previous",
                                    command=self._do_go_back, width=14, state="disabled")
        self._btn_back.pack(side=tk.RIGHT)

        # MQTT log
        log_frame = ttk.LabelFrame(tab, text="MQTT Log", padding=4)
        log_frame.grid(row=4, column=0, sticky="ew")
        log_frame.columnconfigure(0, weight=1)

        self._log_text = tk.Text(log_frame, height=3, wrap=tk.WORD, state="disabled",
                                 background="#1e1e1e", foreground="#cccccc",
                                 font=("Courier", 9))
        self._log_text.grid(row=0, column=0, sticky="ew")

    # ── Overview tab ──────────────────────────────────────────────────────────

    def _build_overview_tab(self):
        tab = self.tab_overview
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)

        cols = ("Mode", "Fan", "Temperature", "Status")
        self._overview_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                           selectmode="browse")
        for col in cols:
            self._overview_tree.heading(col, text=col)
            self._overview_tree.column(col, width=160, anchor="center")

        vsb = ttk.Scrollbar(tab, orient="vertical", command=self._overview_tree.yview)
        self._overview_tree.configure(yscrollcommand=vsb.set)
        self._overview_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self._overview_tree.tag_configure("captured", background="#c8e6c9")  # green
        self._overview_tree.tag_configure("skipped",  background="#fff9c4")  # yellow
        self._overview_tree.tag_configure("pending",  background="#ffffff")  # white

        self._overview_tree.bind("<Double-1>", self._overview_jump)

        info = ttk.Label(tab, text="Double-click any row to re-capture it.", foreground="gray")
        info.grid(row=1, column=0, sticky="w", pady=(4, 0))

        ttk.Button(tab, text="🔄  Refresh", command=self._refresh_overview).grid(
            row=1, column=0, sticky="e", pady=(4, 0))

    # ── Export tab ────────────────────────────────────────────────────────────

    def _build_export_tab(self):
        tab = self.tab_export
        tab.columnconfigure(1, weight=1)

        # Summary
        ttk.Label(tab, text="Summary:", font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=0, sticky="nw", pady=(0, 4))
        self._export_summary_var = tk.StringVar(value="—")
        ttk.Label(tab, textvariable=self._export_summary_var, justify="left").grid(
            row=0, column=1, columnspan=2, sticky="nw", pady=(0, 4))

        # Output path
        ttk.Label(tab, text="Output file:").grid(row=1, column=0, sticky="w", pady=4)
        self._export_path_var = tk.StringVar()
        ttk.Entry(tab, textvariable=self._export_path_var).grid(
            row=1, column=1, sticky="ew", padx=(6, 4))
        ttk.Button(tab, text="Browse…", command=self._browse_export_path).grid(row=1, column=2)

        # Test sub-panel
        test_frame = ttk.LabelFrame(tab, text="Test a Code (live replay)", padding=8)
        test_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 6))
        test_frame.columnconfigure(1, weight=1)
        test_frame.columnconfigure(3, weight=1)
        test_frame.columnconfigure(5, weight=1)
        test_frame.columnconfigure(7, weight=1)

        ttk.Label(test_frame, text="Mode:").grid(row=0, column=0, sticky="w")
        self._test_mode_var = tk.StringVar()
        self._test_mode_cb = ttk.Combobox(test_frame, textvariable=self._test_mode_var, width=12)
        self._test_mode_cb.grid(row=0, column=1, padx=(4, 10))

        ttk.Label(test_frame, text="Fan:").grid(row=0, column=2, sticky="w")
        self._test_fan_var = tk.StringVar()
        self._test_fan_cb = ttk.Combobox(test_frame, textvariable=self._test_fan_var, width=10)
        self._test_fan_cb.grid(row=0, column=3, padx=(4, 10))

        ttk.Label(test_frame, text="Swing:").grid(row=0, column=4, sticky="w")
        self._test_swing_var = tk.StringVar()
        self._test_swing_cb = ttk.Combobox(test_frame, textvariable=self._test_swing_var, width=10)
        self._test_swing_cb.grid(row=0, column=5, padx=(4, 10))

        ttk.Label(test_frame, text="Temp:").grid(row=0, column=6, sticky="w")
        self._test_temp_var = tk.StringVar()
        self._test_temp_cb = ttk.Combobox(test_frame, textvariable=self._test_temp_var, width=8)
        self._test_temp_cb.grid(row=0, column=7, padx=(4, 10))

        ttk.Button(test_frame, text="▶  Send", command=self._do_test).grid(row=0, column=8)

        ttk.Label(test_frame, text="Leave Swing empty if model has no swing.",
                  foreground="gray").grid(row=1, column=0, columnspan=9, sticky="w", pady=(4, 0))

        # Export button
        ttk.Separator(tab).grid(row=3, column=0, columnspan=3, sticky="ew", pady=8)
        ttk.Button(tab, text="💾  Export JSON", command=self._do_export).grid(
            row=4, column=0, columnspan=3)
        self._export_status_var = tk.StringVar()
        ttk.Label(tab, textvariable=self._export_status_var, foreground="green").grid(
            row=5, column=0, columnspan=3, pady=(6, 0))

    # ─────────────────────────────────────────────────────────────────────────
    # Session
    # ─────────────────────────────────────────────────────────────────────────

    def _load_session(self):
        path = filedialog.askopenfilename(
            title="Load session file",
            initialdir=TOOLS_DIR,
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
            initialfile="capture_session.json",
        )
        if not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
            cfg = data["config"]
            self._sv["mqtt_host"].set(cfg.get("mqtt_host", ""))
            self._sv["mqtt_port"].set(str(cfg.get("mqtt_port", 1883)))
            self._sv["mqtt_user"].set(cfg.get("mqtt_user", ""))
            self._sv["mqtt_pass"].set(cfg.get("mqtt_pass", ""))
            self._sv["device_name"].set(cfg.get("device_name", ""))
            self._sv["manufacturer"].set(cfg.get("manufacturer", ""))
            self._sv["models"].set(", ".join(cfg.get("supportedModels", [])))
            self._sv["min_temp"].set(str(cfg.get("minTemperature", 16)))
            self._sv["max_temp"].set(str(cfg.get("maxTemperature", 30)))
            self._sv["precision"].set(str(cfg.get("precision", 1)))
            self._sv["op_modes"].set(", ".join(cfg.get("operationModes", [])))
            self._sv["fan_modes"].set(", ".join(cfg.get("fanModes", [])))
            self._sv["swing_modes"].set(", ".join(cfg.get("swingModes", [])))
            self._sv["single_code_modes"].set(", ".join(cfg.get("singleCodeModes", [])))
            self._sv["excluded_modes"].set(", ".join(cfg.get("excludedModes", [])))
            self.session = data
            n = len(data.get("codes", {}))
            self._setup_status_var.set(f"✓ Session loaded — {n} code(s) captured so far.")
        except Exception as exc:
            messagebox.showerror("Load error", str(exc))

    def _save_session(self):
        tmp = SESSION_FILE.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(self.session, f, indent=2)
            os.replace(tmp, SESSION_FILE)
        except Exception as exc:
            self._log(f"[WARN] Session save failed: {exc}")

    def _parse_config(self) -> dict | None:
        try:
            host   = self._sv["mqtt_host"].get().strip()
            port   = int(self._sv["mqtt_port"].get().strip())
            user   = self._sv["mqtt_user"].get().strip()
            pw     = self._sv["mqtt_pass"].get()
            device = self._sv["device_name"].get().strip()
            mfr    = self._sv["manufacturer"].get().strip()
            models = [m.strip() for m in self._sv["models"].get().split(",") if m.strip()]
            min_t  = float(self._sv["min_temp"].get().strip())
            max_t  = float(self._sv["max_temp"].get().strip())
            prec   = float(self._sv["precision"].get().strip())
            op_modes  = [m.strip() for m in self._sv["op_modes"].get().split(",")  if m.strip()]
            fan_modes = [m.strip() for m in self._sv["fan_modes"].get().split(",") if m.strip()]
            sw_raw    = self._sv["swing_modes"].get().strip()
            swing_modes = [m.strip() for m in sw_raw.split(",") if m.strip()] if sw_raw else []
            sc_raw    = self._sv["single_code_modes"].get().strip()
            single_code_modes = [m.strip() for m in sc_raw.split(",") if m.strip()] if sc_raw else []
            ex_raw    = self._sv["excluded_modes"].get().strip()
            excluded_modes = [m.strip() for m in ex_raw.split(",") if m.strip()] if ex_raw else []

            if not all([host, device, mfr, models, op_modes, fan_modes]):
                raise ValueError("All required fields must be filled.")
            if min_t >= max_t:
                raise ValueError("Min temperature must be less than max temperature.")
            if prec not in (1, 0.5):
                raise ValueError("Precision must be 1 or 0.5.")

            return {
                "mqtt_host": host, "mqtt_port": port,
                "mqtt_user": user, "mqtt_pass": pw,
                "device_name": device,
                "manufacturer": mfr, "supportedModels": models,
                "minTemperature": min_t, "maxTemperature": max_t, "precision": prec,
                "operationModes": op_modes, "fanModes": fan_modes, "swingModes": swing_modes,
                "singleCodeModes": single_code_modes,
                "excludedModes": excluded_modes,
            }
        except ValueError as exc:
            messagebox.showerror("Config error", str(exc))
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # MQTT
    # ─────────────────────────────────────────────────────────────────────────

    def _start_capture(self):
        if not PAHO_AVAILABLE:
            messagebox.showerror("Error", "paho-mqtt is not installed.\n\nRun: pip install paho-mqtt")
            return
        config = self._parse_config()
        if not config:
            return

        existing_codes = self.session.get("codes", {})
        self.session = {"config": config, "codes": existing_codes}
        self._friendly_name = config["device_name"]
        self.combos = build_combos(config)
        self.current_idx = 0
        self._save_session()
        self._connect_mqtt(config)

    def _connect_mqtt(self, config: dict):
        self._setup_status_var.set("Connecting to MQTT broker…")

        def on_connect(client, userdata, flags, rc, properties=None):
            # rc may be int (paho v1) or ReasonCode (paho v2)
            success = (rc == 0) if isinstance(rc, int) else (not rc.is_failure)
            if success:
                client.subscribe(f"zigbee2mqtt/{self._friendly_name}")
                self.after(0, self._on_mqtt_connected)
            else:
                err = str(rc)
                self.after(0, lambda: self._setup_status_var.set(f"MQTT connect failed: {err}"))

        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode())
                code = payload.get("learned_ir_code")
                if code and self.listening:
                    self.after(0, lambda c=code: self._on_code_received(c))
            except Exception:
                pass

        def on_disconnect(client, userdata, *args):
            self.after(0, lambda: self._log("[WARN] MQTT disconnected"))

        # Support paho v1 and v2
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:
            client = mqtt.Client()  # type: ignore[call-arg]

        if config["mqtt_user"]:
            client.username_pw_set(config["mqtt_user"], config["mqtt_pass"])
        client.on_connect    = on_connect
        client.on_message    = on_message
        client.on_disconnect = on_disconnect

        try:
            client.connect(config["mqtt_host"], config["mqtt_port"], keepalive=60)
        except Exception as exc:
            self._setup_status_var.set(f"Connection error: {exc}")
            return

        self.mqtt_client = client
        threading.Thread(target=client.loop_forever, daemon=True).start()

    def _on_mqtt_connected(self):
        self._setup_status_var.set("✓ Connected to MQTT broker")
        for tab_idx in (1, 2, 3):
            self.notebook.tab(tab_idx, state="normal")
        self.notebook.select(1)

        cfg = self.session["config"]
        self._test_mode_cb["values"]  = cfg["operationModes"]
        self._test_fan_cb["values"]   = cfg["fanModes"]
        self._test_swing_cb["values"] = cfg.get("swingModes", [])
        self._test_temp_cb["values"]  = gen_temps(
            cfg["minTemperature"], cfg["maxTemperature"], cfg["precision"])

        self._advance_to_next()
        self._refresh_overview()
        self._refresh_export_summary()

    def _publish(self, payload: dict):
        if self.mqtt_client:
            topic = f"zigbee2mqtt/{self._friendly_name}/set"
            self.mqtt_client.publish(topic, json.dumps(payload))
            self._log(f"→ {topic}  {json.dumps(payload)[:80]}")

    # ─────────────────────────────────────────────────────────────────────────
    # Capture flow
    # ─────────────────────────────────────────────────────────────────────────

    def _current_combo(self) -> dict | None:
        if self.current_idx < len(self.combos):
            return self.combos[self.current_idx]
        return None

    def _advance_to_next(self):
        """Advance current_idx past already-captured/skipped combos."""
        codes = self.session.get("codes", {})
        while self.current_idx < len(self.combos):
            if combo_key(self.combos[self.current_idx]) not in codes:
                break
            self.current_idx += 1
        self._update_capture_ui()
        self._update_progress()

    # Descriptions shown under each field value in the target breakdown
    _OP_MODE_DESC = {
        "cool":      "Cooling",
        "heat":      "Heating",
        "dry":       "Dehumidify",
        "fan_only":  "Fan only",
        "auto":      "Automatic",
        "heat_cool": "Heat + cool",
    }
    _FAN_MODE_DESC = {
        "auto":      "Automatic speed",
        "quiet":     "Silent / quiet",
        "low":       "Low speed",
        "mid":       "Medium speed",
        "med":       "Medium speed",
        "high":      "High speed",
        "highest":   "Max speed",
        "turbo":     "Turbo / boost",
    }

    def _update_capture_ui(self):
        combo = self._current_combo()
        if combo is None:
            self._combo_label_var.set("All done! 🎉")
            self._status_var.set("All combinations captured. Export your codes from the Export tab.")
            self._status_label.configure(foreground="green")
            self._btn_capture.configure(state="disabled")
            self._btn_skip.configure(state="disabled")
            for hv, vv in self._breakdown_cells:
                hv.set(""); vv.set("")
            self.notebook.select(3)
            self._refresh_export_summary()
            return

        self._combo_label_var.set(combo_label(combo))
        self._status_var.set("Press ▶ Capture, then press the remote button.")
        self._status_label.configure(foreground="gray")
        self._btn_capture.configure(state="normal")
        self._btn_confirm.configure(state="disabled")
        self._btn_retry.configure(state="disabled")
        self._btn_copy.configure(state="disabled")
        self._btn_skip.configure(state="normal")
        self.pending_code = None
        self._set_code_preview("")

        # Build labeled breakdown
        if combo["type"] == "off":
            fields = [
                ("Action", "OFF", "Power-off signal"),
            ]
        else:
            mode = combo["mode"]
            fan  = combo["fan"]
            temp = combo["temp"]
            mode_desc = self._OP_MODE_DESC.get(mode.lower(), "")
            fan_desc  = self._FAN_MODE_DESC.get(fan.lower(), "")
            fields = [
                ("Operation Mode",  mode,          mode_desc),
                ("Fan Speed",        fan,           fan_desc),
            ]
            if "swing" in combo:
                fields.append(("Swing Position", combo["swing"], "Vane direction"))
            if combo.get("single_code"):
                fields.append(("Temperature", "all temps", "One code → fills all temperatures"))
            else:
                fields.append(("Temperature", f"{temp} °C", "Target temperature"))

        for i, cell in enumerate(self._breakdown_cells):
            hv, vv = cell
            if i < len(fields):
                heading, value, desc = fields[i]
                hv.set(f"{heading}:")
                vv.set(f"{value}" + (f"  ({desc})" if desc else ""))
            else:
                hv.set(""); vv.set("")

    def _update_progress(self):
        total = len(self.combos)
        done  = len(self.session.get("codes", {}))
        self._progress_label_var.set(f"{done} / {total}")
        self._progress_bar.configure(maximum=max(total, 1), value=done)
        # Enable back button only when there's something to go back to
        has_prev = any(
            combo_key(c) in self.session.get("codes", {})
            for c in self.combos[:max(self.current_idx, 1)]
        )
        self._btn_back.configure(state="normal" if has_prev else "disabled")

    def _do_capture(self):
        combo = self._current_combo()
        if not combo:
            return
        self.listening = True
        self._status_var.set("⏳  Listening for IR signal… (30 s timeout)")
        self._status_label.configure(foreground="steelblue")
        self._btn_capture.configure(state="disabled")
        self._publish({"learn_ir_code": "ON"})
        self._log(f"[INFO] Listening for: {combo_label(combo)}")
        self._listen_timer = self.after(30_000, self._on_listen_timeout)

    def _on_listen_timeout(self):
        if self.listening:
            self.listening = False
            self._status_var.set("⚠  No code received (timed out). Try again.")
            self._status_label.configure(foreground="orange")
            self._btn_capture.configure(state="normal")

    def _on_code_received(self, code: str):
        if not self.listening:
            return
        self.listening = False
        if self._listen_timer is not None:
            self.after_cancel(self._listen_timer)
            self._listen_timer = None

        self.pending_code = code
        self._status_var.set("✓  Code received!  Confirm or Retry.")
        self._status_label.configure(foreground="green")
        self._set_code_preview(code)
        combo = self._current_combo()
        can_copy = combo is not None and combo["type"] != "off" and not combo.get("single_code")
        self._btn_confirm.configure(state="normal")
        self._btn_retry.configure(state="normal")
        self._btn_copy.configure(state="normal" if can_copy else "disabled")
        self._btn_capture.configure(state="disabled")
        self._log(f"[OK]   Code received ({len(code)} chars)")

    def _do_confirm(self):
        combo = self._current_combo()
        if not combo or not self.pending_code:
            return
        # SmartIR MQTT controller publishes the command string verbatim as payload.
        # zigbee2mqtt expects {"ir_code_to_send": "..."} on the /set topic.
        stored = json.dumps({"ir_code_to_send": self.pending_code})
        self.session.setdefault("codes", {})[combo_key(combo)] = stored
        self._save_session()
        self._log(f"[SAVE] {combo_key(combo)}")
        self.current_idx += 1
        self._advance_to_next()
        self._refresh_overview()
        self._refresh_export_summary()

    def _do_retry(self):
        self.pending_code = None
        self._set_code_preview("")
        self._btn_confirm.configure(state="disabled")
        self._btn_retry.configure(state="disabled")
        self._btn_copy.configure(state="disabled")
        self._do_capture()

    def _do_copy_remaining(self):
        """Apply current pending code to ALL remaining temps in the same mode/fan[/swing] group."""
        combo = self._current_combo()
        if not combo or not self.pending_code or combo["type"] == "off":
            return
        stored = json.dumps({"ir_code_to_send": self.pending_code})
        codes = self.session.setdefault("codes", {})

        # Save current
        codes[combo_key(combo)] = stored

        filled = 0
        for i in range(self.current_idx + 1, len(self.combos)):
            c = self.combos[i]
            if c["type"] != "code":
                continue
            if c["mode"] != combo["mode"] or c["fan"] != combo["fan"]:
                continue
            if "swing" in combo and c.get("swing") != combo.get("swing"):
                continue
            if combo_key(c) not in codes:
                codes[combo_key(c)] = stored
                filled += 1

        self._save_session()
        self._log(f"[COPY] Applied to {filled} additional temperature(s) in same group.")
        self.current_idx += 1
        self._advance_to_next()
        self._refresh_overview()
        self._refresh_export_summary()

    def _do_skip(self):
        combo = self._current_combo()
        if not combo:
            return
        self.session.setdefault("codes", {})[combo_key(combo)] = _SKIPPED
        self._save_session()
        self._log(f"[SKIP] {combo_key(combo)}")
        self.current_idx += 1
        self._advance_to_next()
        self._refresh_overview()
        self._refresh_export_summary()

    def _do_go_back(self):
        """Remove the most recently captured/skipped code and jump back to re-capture it."""
        codes = self.session.get("codes", {})
        # Find the last combo (by index) that has a code stored
        prev_idx = None
        for i in range(self.current_idx - 1, -1, -1):
            if combo_key(self.combos[i]) in codes:
                prev_idx = i
                break
        if prev_idx is None:
            return
        key = combo_key(self.combos[prev_idx])
        codes.pop(key, None)
        self._save_session()
        self._log(f"[BACK] Removed {key}, returning to re-capture")
        self.current_idx = prev_idx
        self.listening = False
        if self._listen_timer is not None:
            self.after_cancel(self._listen_timer)
            self._listen_timer = None
        self._update_capture_ui()
        self._update_progress()
        self._refresh_overview()
        self._refresh_export_summary()

    def _set_code_preview(self, text: str):
        self._code_preview.configure(state="normal")
        self._code_preview.delete("1.0", tk.END)
        if text:
            preview = text[:300] + ("…" if len(text) > 300 else "")
            self._code_preview.insert("1.0", preview)
        self._code_preview.configure(state="disabled")

    def _log(self, msg: str):
        def _update():
            self._log_text.configure(state="normal")
            self._log_text.insert(tk.END, msg + "\n")
            lines = int(self._log_text.index(tk.END).split(".")[0])
            if lines > 60:
                self._log_text.delete("1.0", f"{lines - 60}.0")
            self._log_text.see(tk.END)
            self._log_text.configure(state="disabled")
        self.after(0, _update)

    # ─────────────────────────────────────────────────────────────────────────
    # Overview
    # ─────────────────────────────────────────────────────────────────────────

    def _refresh_overview(self):
        tree = self._overview_tree
        tree.delete(*tree.get_children())

        codes   = self.session.get("codes", {})
        cfg     = self.session.get("config", {})
        has_swing = bool(cfg.get("swingModes"))

        if has_swing:
            tree.configure(columns=("Mode", "Fan", "Swing", "Temperature", "Status"))
            widths = (130, 100, 130, 120, 100)
        else:
            tree.configure(columns=("Mode", "Fan", "Temperature", "Status"))
            widths = (160, 130, 160, 120)

        for col, w in zip(tree["columns"], widths):
            tree.heading(col, text=col)
            tree.column(col, width=w, anchor="center")

        for combo in self.combos:
            key = combo_key(combo)
            val = codes.get(key)
            if val is None:
                status, tag = "pending",  "pending"
            elif val == _SKIPPED:
                status, tag = "skipped",  "skipped"
            else:
                status, tag = "captured", "captured"

            if combo["type"] == "off":
                row = ("—", "—", "—", "OFF", status) if has_swing else ("—", "—", "OFF", status)
            else:
                if has_swing:
                    row = (combo["mode"], combo["fan"], combo.get("swing", ""), combo["temp"], status)
                else:
                    row = (combo["mode"], combo["fan"], combo["temp"], status)

            tree.insert("", tk.END, iid=key, values=row, tags=(tag,))

    def _overview_jump(self, event):
        """Double-click a row to re-capture that combination."""
        sel = self._overview_tree.selection()
        if not sel:
            return
        key = sel[0]
        for i, combo in enumerate(self.combos):
            if combo_key(combo) == key:
                # Remove from codes so it counts as pending
                self.session.get("codes", {}).pop(key, None)
                self.current_idx = i
                self._update_capture_ui()
                self._update_progress()
                self.notebook.select(1)
                break

    # ─────────────────────────────────────────────────────────────────────────
    # Export
    # ─────────────────────────────────────────────────────────────────────────

    def _refresh_export_summary(self):
        codes   = self.session.get("codes", {})
        total   = len(self.combos)
        captured = sum(1 for v in codes.values() if v != _SKIPPED)
        skipped  = sum(1 for v in codes.values() if v == _SKIPPED)
        pending  = total - len(codes)
        self._export_summary_var.set(
            f"Total: {total}     Captured: {captured}     Skipped: {skipped}     Pending: {pending}"
        )

        # Auto-suggest an output path on first call
        if not self._export_path_var.get() and CODES_DIR.exists():
            existing = [int(f.stem) for f in CODES_DIR.glob("*.json") if f.stem.isdigit()]
            next_code = max(existing) + 1 if existing else 9000
            self._export_path_var.set(str(CODES_DIR / f"{next_code}.json"))

    def _browse_export_path(self):
        initial = CODES_DIR if CODES_DIR.exists() else TOOLS_DIR.parent
        path = filedialog.asksaveasfilename(
            title="Export SmartIR JSON",
            initialdir=initial,
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if path:
            self._export_path_var.set(path)

    def _do_export(self):
        config = self.session.get("config")
        codes  = self.session.get("codes", {})
        path   = self._export_path_var.get().strip()

        if not config:
            messagebox.showerror("Export error", "No active session. Connect and capture first.")
            return
        if not path:
            messagebox.showerror("Export error", "Choose an output file path.")
            return

        captured = {k: v for k, v in codes.items() if v != _SKIPPED}
        if not captured:
            messagebox.showerror("Export error", "No codes captured yet.")
            return

        # Build commands dict from combos
        commands: dict = {}
        missing: list  = []

        all_temps = gen_temps(
            config["minTemperature"], config["maxTemperature"], config["precision"])

        for combo in self.combos:
            key  = combo_key(combo)
            code = captured.get(key)

            if combo["type"] == "off":
                if code:
                    commands["off"] = code
                else:
                    missing.append("off")
                    commands["off"] = ""
                continue

            mode = combo["mode"]
            fan  = combo["fan"]
            temp = combo["temp"]

            if not code:
                missing.append(key)
                code = ""

            # Single-code mode: one capture expands to fill every temperature slot
            temps_to_write = all_temps if combo.get("single_code") else [temp]

            if "swing" in combo:
                swing = combo["swing"]
                for t in temps_to_write:
                    commands.setdefault(mode, {}).setdefault(fan, {}).setdefault(swing, {})[t] = code
            else:
                for t in temps_to_write:
                    commands.setdefault(mode, {}).setdefault(fan, {})[t] = code

        if missing:
            answer = messagebox.askyesno(
                "Missing codes",
                f"{len(missing)} combination(s) have no captured code and will be written as "
                "empty strings. Export anyway?",
            )
            if not answer:
                return

        output = {
            "manufacturer":      config["manufacturer"],
            "supportedModels":   config["supportedModels"],
            "supportedController": "MQTT",
            "commandsEncoding":  "Raw",
            "minTemperature":    config["minTemperature"],
            "maxTemperature":    config["maxTemperature"],
            "precision":         config["precision"],
            "operationModes":    [m for m in config["operationModes"]
                                   if m not in set(config.get("excludedModes", []))],
            "fanModes":          config["fanModes"],
        }
        if config.get("swingModes"):
            output["swingModes"] = config["swingModes"]
        output["commands"] = commands

        try:
            out = Path(path)
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(output, f, indent=2)
            os.replace(tmp, out)
            self._export_status_var.set(f"✓  Exported → {out.name}")
            self._log(f"[EXPORT] Written to {out}")
        except Exception as exc:
            messagebox.showerror("Export error", str(exc))

    def _do_test(self):
        mode  = self._test_mode_var.get()
        fan   = self._test_fan_var.get()
        swing = self._test_swing_var.get().strip()
        temp  = self._test_temp_var.get()

        if not mode or not fan or not temp:
            messagebox.showwarning("Test", "Select at least Mode, Fan, and Temperature.")
            return

        cfg = self.session.get("config", {})
        has_swing = bool(cfg.get("swingModes"))
        if has_swing and not swing:
            messagebox.showwarning("Test", "This session has swing modes — select one.")
            return

        key = f"{mode}|{fan}|{swing}|{temp}" if (has_swing and swing) else f"{mode}|{fan}|{temp}"
        stored = self.session.get("codes", {}).get(key)

        if not stored or stored == _SKIPPED:
            messagebox.showwarning("Test", f"No captured code for: {key}")
            return

        try:
            payload = json.loads(stored)
        except Exception:
            messagebox.showerror("Test", f"Stored value is not valid JSON: {stored[:80]}")
            return

        if not self.mqtt_client:
            messagebox.showerror("Test", "MQTT not connected.")
            return

        topic = f"zigbee2mqtt/{self._friendly_name}/set"
        self.mqtt_client.publish(topic, json.dumps(payload))
        self._log(f"[TEST]  Sent {key} → {topic}")

    # ─────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def _on_close(self):
        if self.mqtt_client:
            try:
                self.mqtt_client.disconnect()
            except Exception:
                pass
        self.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = SmartIRCapture()
    app.mainloop()


if __name__ == "__main__":
    main()
