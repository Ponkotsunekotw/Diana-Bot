"""
Diana Bot Control Center (Responsive & Adaptive Edition)
========================================================
A modular, dark-themed, fully responsive desktop control center for installing,
configuring, launching, and observing Diana Bot without altering her core runtime workflow.

Architectural Overview (Separation of Concerns & State-Driven Broadcast Pattern):
1. State, Viewport & Signal Layer (`RuntimeLifecycleState`, `ViewportLayoutState`, `BroadcastSignal`):
   - Tracks explicit lifecycle flags (`is_bot_running_flag`, `is_testing_local_api_flag`,
     `is_testing_web_api_flag`, `is_testing_image_api_flag`, `is_generating_persona_flag`,
     `is_installing_deps_flag`, `is_running_evolution_flag`, `is_reflowing_layout_flag`)
     and deterministically resets each flag to `False` (IDLE baseline) upon completion.
   - Evaluates window dimensions via `ViewportBreakpointController` (`compact` / `standard` / `wide`)
     and dispatches debounced responsive reflow updates across all pages.
   - Implements a thread-safe Publisher-Subscriber signal queue (`signal_queue`)
     bridging background workers and the Tkinter UI thread.
2. Configuration & Data Repository (`ConfigRepository`):
   - Encapsulates atomic JSON reads/writes and preserves strict separation of the
     three API key files (`chat`, `evolution`, `image`) plus `discord_token.txt`.
3. Execution / Worker Layer (`ExecutionWorkers`):
   - Decoupled background workers for subprocess management (`bot.py`, `pip`,
     `diana_evolution.py`, `patch_pusher.py`), OpenAI/Gemini endpoint health probes,
     and LLM persona generation. Workers never mutate UI widgets directly; they emit signals.
4. Presentation / Adaptive UI Layer (`DianaControlCenterApp` & Responsive Components):
   - Screen-aware initial geometry (auto-scales to fit 720p/1080p/1440p/4K & High-DPI displays).
   - Collapsible Navigation Rail (auto-collapses to 68px icon rail on compact widths or manual toggle).
   - Dynamic Breakpoint Grid Reflow:
     * Dashboard KPI cards reflow between 4-column (`1x4`) and 2-column (`2x2`) grids.
     * Dashboard top telemetry & bottom terminal console are hosted in a draggable `tk.PanedWindow`.
     * Dashboard toolbar reflows between 1-row and 2-row layouts so buttons and search never collide.
     * `PipelineStepperCanvas` scales node coordinates, radii, and fonts proportionally to actual width.
     * Connections, Persona Studio, and System Diagnostics pages use `ResponsiveScrollFrame` and
       dynamic 1-column / 2-column reflow so forms never clip on compact laptop screens.
"""

from __future__ import annotations

import ctypes
import dataclasses
import importlib.util
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
import zipfile
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 0. Path Constants & Default Schema
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
IMG_DIR = ROOT / "img"
REFERENCE_IMG_DIR = IMG_DIR / "reference"

GUI_CONFIG = CONFIG / "gui_config.json"
PERSONA = CONFIG / "character_persona.json"
DIANA_EXAMPLE = CONFIG / "persona_diana_example.json"
ACTION_IMAGES = CONFIG / "action_images.json"
PATCH_PUSHER_SCRIPT = ROOT / "patch_pusher.py"
PATCH_PUSHER_EXE = ROOT / "DianaPatchPusher.exe"
SEED_INSTALLER_EXE = ROOT / "DianaSeedInstaller.exe"
SEED_INSTALLER_BUILD_BAT = ROOT / "installer" / "build_installer.bat"
DEFAULT_SEED_FILE = ROOT / "Diana-Bot.seed"

TOKEN = CONFIG / "discord_token.txt"
CHAT_KEY = CONFIG / "google_api_key_chat.txt"
EVOLUTION_KEY = CONFIG / "google_api_key_evolution.txt"
IMAGE_KEY = CONFIG / "google_api_key_image.txt"

STATUS_FILE = DATA / "runtime_status.json"
VERSION_FILE = DATA / "version.json"
BOT_STATE_FILE = DATA / "bot_state.json"
FAVORABILITY_FILE = DATA / "favorability.json"
GUEST_NOTES_FILE = DATA / "guest_notes.json"
REMINDERS_FILE = DATA / "reminders.json"
USER_PROFILE_FILE = DATA / "user_profile.json"
MEMORY_FILE = DATA / "memory.json"
BASE_FILE = DATA / "base.json"
CHROMA_DB_DIR = ROOT / "diana_memory_db"
CHROMA_SQLITE_FILE = CHROMA_DB_DIR / "chroma.sqlite3"

DEFAULT_CFG: Dict[str, Any] = {
    "local_base_url": "http://127.0.0.1:1234/v1",
    "local_model": "google/gemma-4-e2b",
    "web_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "web_model": "gemini-3.5-flash-lite",
    "master_backend": "local",
    "master_model": "google/gemma-4-e2b",
    "guest_backend": "local",
    "guest_model": "google/gemma-4-e2b",
    "image_endpoint": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image:generateContent",
    "image_model": "gemini-3.1-flash-image",
    "temperature": 0.75,
    "thinking_tokens": 256,
}

PIPELINE_STAGES: List[Tuple[str, str, str]] = [
    ("idle", "等待待命", "Idle"),
    ("analyzing", "訊息分析", "Analyze"),
    ("memory", "記憶檢索", "RAG Memory"),
    ("thinking", "LLM 思考", "Reasoning"),
    ("replying", "訊號回覆", "Discord Reply"),
    ("memory_cleanup", "整理記憶", "Consolidate"),
]

# Design System Palette (Slate / Indigo Modern Dark Theme)
PALETTE = {
    "bg_app": "#0B0F19",        # Deep obsidian background
    "bg_sidebar": "#111827",    # Navigation rail surface
    "bg_card": "#161F30",       # Elevated card surface
    "bg_card_alt": "#1E293B",   # Secondary card / hover surface
    "bg_input": "#0F172A",      # Text entry / console surface
    "border": "#26334D",        # Crisp 1px card border
    "border_focus": "#6366F1",  # Indigo active border
    "accent": "#6366F1",        # Primary Indigo
    "accent_hover": "#4F46E5",  # Deep Indigo hover
    "accent_soft": "#1E1B4B",   # Subtle indigo tint
    "success": "#10B981",       # Emerald green
    "success_bg": "#064E3B",
    "warning": "#F59E0B",       # Amber gold
    "warning_bg": "#451A03",
    "danger": "#EF4444",        # Rose red
    "danger_bg": "#450A0A",
    "cyan": "#06B6D4",          # Cyan telemetry highlight
    "text_primary": "#F8FAFC",  # Crisp white-slate
    "text_secondary": "#94A3B8",# Muted slate label
    "text_muted": "#64748B",    # Dim caption text
}


def enable_high_dpi() -> None:
    """Enable High-DPI scaling on Windows so typography and Canvas vectors render crisply."""
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 1. State Management, Responsive Viewport Logic & Broadcast Signal Layer
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class BroadcastSignal:
    """Explicit signal contract between background execution workers and UI orchestrator."""
    signal_type: str
    payload: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class RuntimeLifecycleState:
    """
    Centralized state tracker for asynchronous tasks, UI reflow, and subprocess lifecycles.
    Every flag is set to `True` when a module is triggered and deterministically
    reset to `False` (baseline IDLE) upon completion.
    """
    is_bot_running_flag: bool = False
    is_testing_local_api_flag: bool = False
    is_testing_web_api_flag: bool = False
    is_testing_image_api_flag: bool = False
    is_generating_persona_flag: bool = False
    is_installing_deps_flag: bool = False
    is_running_evolution_flag: bool = False
    is_reflowing_layout_flag: bool = False
    active_page: str = "dashboard"


@dataclasses.dataclass
class ViewportLayoutState:
    """
    Stores current responsive viewport metrics and computed layout breakpoints.
    """
    window_width: int = 1220
    window_height: int = 800
    sidebar_width: int = 232
    workspace_width: int = 988
    breakpoint: str = "standard"          # 'compact' (<980), 'standard' (980..1360), 'wide' (>1360)
    sidebar_collapsed: bool = False
    sidebar_user_override: Optional[bool] = None
    kpi_columns: int = 4                  # 4 columns on standard/wide, 2 columns on compact
    dual_col_forms: bool = True           # 2 columns on standard/wide, 1 column on compact
    compact_toolbar: bool = False         # 2-row toolbar when workspace width < 820


class ViewportBreakpointController:
    """
    Pure logic controller evaluating window/workspace dimensions and computing
    deterministic responsive layout rules without directly mutating widgets.
    """

    COMPACT_WINDOW_W = 980
    WIDE_WINDOW_W = 1360
    COMPACT_WORKSPACE_W = 860
    SIDEBAR_EXPANDED_RATIO = 0.19         # Expanded sidebar occupies ~19% of window width
    SIDEBAR_COLLAPSED_RATIO = 0.06        # Collapsed sidebar occupies ~6% of window width
    SIDEBAR_EXPANDED_MIN_W = 200
    SIDEBAR_EXPANDED_MAX_W = 320
    SIDEBAR_COLLAPSED_MIN_W = 58
    SIDEBAR_COLLAPSED_MAX_W = 84

    @classmethod
    def compute_initial_geometry(cls, screen_w: int, screen_h: int) -> Tuple[int, int, int, int]:
        """
        Computes an adaptive initial window width, height, and centered (x, y) coordinates
        that fit comfortably on any display (from 1280x720 / High-DPI laptops to 4K monitors).
        """
        safe_w = max(screen_w, 800)
        safe_h = max(screen_h, 600)
        target_w = min(max(int(safe_w * 0.84), 860), 1380)
        target_h = min(max(int(safe_h * 0.82), 560), 860)
        # Ensure window never exceeds 92% of available screen dimensions
        target_w = min(target_w, int(safe_w * 0.94))
        target_h = min(target_h, int(safe_h * 0.90))
        pos_x = max((safe_w - target_w) // 2, 10)
        pos_y = max((safe_h - target_h) // 2 - 16, 10)
        return target_w, target_h, pos_x, pos_y

    @classmethod
    def compute_sidebar_width(cls, win_w: int, collapsed: bool) -> int:
        """Computes sidebar width proportionally to the current window width."""
        if collapsed:
            return min(max(int(win_w * cls.SIDEBAR_COLLAPSED_RATIO), cls.SIDEBAR_COLLAPSED_MIN_W), cls.SIDEBAR_COLLAPSED_MAX_W)
        return min(max(int(win_w * cls.SIDEBAR_EXPANDED_RATIO), cls.SIDEBAR_EXPANDED_MIN_W), cls.SIDEBAR_EXPANDED_MAX_W)

    @classmethod
    def evaluate_viewport(
        cls,
        win_w: int,
        win_h: int,
        current: ViewportLayoutState,
    ) -> Tuple[ViewportLayoutState, bool]:
        """
        Evaluates new window dimensions and returns `(updated_state, has_structural_change)`.
        """
        win_w = max(win_w, 400)
        win_h = max(win_h, 320)

        if win_w < cls.COMPACT_WINDOW_W:
            bp = "compact"
        elif win_w >= cls.WIDE_WINDOW_W:
            bp = "wide"
        else:
            bp = "standard"

        if current.sidebar_user_override is not None:
            collapsed = current.sidebar_user_override
        else:
            collapsed = bp == "compact"

        sidebar_w = cls.compute_sidebar_width(win_w, collapsed)
        ws_w = max(win_w - sidebar_w, 360)

        kpi_cols = 2 if ws_w < cls.COMPACT_WORKSPACE_W else 4
        dual_forms = ws_w >= cls.COMPACT_WORKSPACE_W
        compact_tb = ws_w < 860

        changed = (
            bp != current.breakpoint
            or collapsed != current.sidebar_collapsed
            or abs(sidebar_w - current.sidebar_width) >= 2
            or kpi_cols != current.kpi_columns
            or dual_forms != current.dual_col_forms
            or compact_tb != current.compact_toolbar
            or abs(ws_w - current.workspace_width) >= 16
        )

        new_state = ViewportLayoutState(
            window_width=win_w,
            window_height=win_h,
            sidebar_width=sidebar_w,
            workspace_width=ws_w,
            breakpoint=bp,
            sidebar_collapsed=collapsed,
            sidebar_user_override=current.sidebar_user_override,
            kpi_columns=kpi_cols,
            dual_col_forms=dual_forms,
            compact_toolbar=compact_tb,
        )
        return new_state, changed


# ---------------------------------------------------------------------------
# 2. Data & Configuration Repository Layer
# ---------------------------------------------------------------------------
class ConfigRepository:
    """Encapsulates all file I/O with atomic writes and credential separation."""

    @staticmethod
    def ensure_dirs() -> None:
        CONFIG.mkdir(parents=True, exist_ok=True)
        DATA.mkdir(parents=True, exist_ok=True)
        # Ensure at least one default .persona file exists in config/ for quick-switching
        try:
            if not list(CONFIG.glob("*.persona")):
                src_persona = PERSONA if PERSONA.exists() else DIANA_EXAMPLE
                if src_persona.exists():
                    shutil.copy2(src_persona, CONFIG / "黛安娜.persona")
        except Exception:
            pass

    @staticmethod
    def read_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            if isinstance(default, dict):
                return default.copy()
            if isinstance(default, list):
                return list(default)
            return default

    @staticmethod
    def write_json(path: Path, obj: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def read_first_line(path: Path) -> str:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            return lines[0].strip() if lines else ""
        except Exception:
            return ""

    @staticmethod
    def read_second_line(path: Path) -> str:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            return lines[1].strip() if len(lines) > 1 else ""
        except Exception:
            return ""

    @staticmethod
    def write_first_line_preserve_rest(
        path: Path,
        first_line_val: str,
        second_line_override: Optional[str] = None,
    ) -> None:
        """
        Writes the first-line API key while preserving any subsequent lines
        (such as the evolution model stored on line 2 of google_api_key_evolution.txt).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        existing_lines: List[str] = []
        if path.exists():
            try:
                existing_lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                existing_lines = []

        rest = existing_lines[1:] if len(existing_lines) > 1 else []
        if second_line_override is not None:
            second_clean = second_line_override.strip()
            if rest:
                rest[0] = second_clean
            elif second_clean:
                rest = [second_clean]

        output_lines = [first_line_val.strip()] + rest
        path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")

    @classmethod
    def load_gui_config(cls) -> Dict[str, Any]:
        return {**DEFAULT_CFG, **cls.read_json(GUI_CONFIG, {})}

    @classmethod
    def load_active_persona(cls) -> Dict[str, Any]:
        """Loads active `character_persona.json`, falling back to `persona_diana_example.json`."""
        if PERSONA.exists():
            data = cls.read_json(PERSONA, {})
            if isinstance(data, dict) and data:
                return data
        return cls.read_json(DIANA_EXAMPLE, {"name": "黛安娜"})

    @classmethod
    def load_diana_example_persona(cls) -> Dict[str, Any]:
        return cls.read_json(DIANA_EXAMPLE, cls.load_active_persona())

    @classmethod
    def list_persona_files(cls) -> List[Tuple[Path, Dict[str, Any]]]:
        """Scans ONLY the `config/` directory for `.persona` files (which are JSON under the hood)."""
        cls.ensure_dirs()
        results: List[Tuple[Path, Dict[str, Any]]] = []
        for p in sorted(CONFIG.glob("*.persona"), key=lambda x: x.name.lower()):
            if not p.is_file():
                continue
            data = cls.read_json(p, {})
            if isinstance(data, dict):
                results.append((p, data))
        return results

    @classmethod
    def collect_guest_relationships(cls) -> List[Dict[str, Any]]:
        """
        Aggregates all guests (strictly excluding Master `master_id`) with their
        simple relationship status, favorability, event memories, and object cognition notes.
        """
        base_data = cls.read_json(BASE_FILE, {})
        master_id = str(base_data.get("master_id", "")).strip() if isinstance(base_data, dict) else ""

        guest_notes = cls.read_json(GUEST_NOTES_FILE, {})
        if not isinstance(guest_notes, dict):
            guest_notes = {}

        favor_data = cls.read_json(FAVORABILITY_FILE, {})
        if not isinstance(favor_data, dict):
            favor_data = {}

        short_mem = cls.read_json(MEMORY_FILE, {})
        if not isinstance(short_mem, dict):
            short_mem = {}

        guests_by_key: Dict[str, Dict[str, Any]] = {}

        def _tier_from_score(score: int) -> str:
            if score < 35:
                return "陌生 / 保持戒心"
            if score < 60:
                return "一般訪客 / 認識的客人"
            if score < 80:
                return "友善訪客 / 熟識朋友"
            return "親近友人 / 高度信賴"

        # 1. Load explicit guest records from data/guest_notes.json
        for k, val in guest_notes.items():
            k_str = str(k).strip()
            if not k_str or (master_id and k_str == master_id) or k_str in ("主人", "master"):
                continue
            if isinstance(val, dict):
                uid = str(val.get("user_id", k_str)).strip()
                if master_id and uid == master_id:
                    continue
                name = str(val.get("name", k_str)).strip() or k_str
                fav = int(val.get("favorability", favor_data.get(uid, 50)))
                rel = str(val.get("relationship", "")).strip() or _tier_from_score(fav)
                evts = val.get("events", [])
                if not isinstance(evts, list):
                    evts = [str(evts)] if evts else []
                note = str(val.get("cognition_note", "")).strip() or "對主人保持禮貌的訪客"
                guests_by_key[k_str] = {
                    "key": k_str,
                    "user_id": uid,
                    "name": name,
                    "favorability": fav,
                    "relationship": rel,
                    "events": [str(x) for x in evts if str(x).strip()],
                    "cognition_note": note,
                    "last_seen": str(val.get("last_seen", "")),
                }

        # 2. Scan short-term memory.json for guest turns like "[訪客小明 說]: ..."
        guest_turn_re = re.compile(r"^\[(?:訪客\s+)?([^\]（(]+?)(?:（[^）]*）)?\s*說\]:\s*(.+)$")
        for ch_id, turns in short_mem.items():
            if not isinstance(turns, list):
                continue
            for turn in turns:
                if not isinstance(turn, dict) or turn.get("role") != "user":
                    continue
                content = turn.get("content", "")
                txt = ""
                if isinstance(content, str):
                    txt = content
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            txt = str(part.get("text", ""))
                            break
                m = guest_turn_re.match(txt.strip())
                if m:
                    g_name = m.group(1).strip()
                    g_msg = m.group(2).strip()
                    if g_name in ("主人", "master") or (master_id and g_name == master_id):
                        continue
                    # Find existing entry by name or create one
                    target_key = None
                    for ek, ev in guests_by_key.items():
                        if ev["name"] == g_name or ek == g_name:
                            target_key = ek
                            break
                    if not target_key:
                        target_key = g_name
                        fav_val = int(favor_data.get(str(ch_id), 50))
                        guests_by_key[target_key] = {
                            "key": target_key,
                            "user_id": str(ch_id),
                            "name": g_name,
                            "favorability": fav_val,
                            "relationship": _tier_from_score(fav_val),
                            "events": [],
                            "cognition_note": f"曾在頻道 {ch_id} 與黛安娜交談的訪客",
                            "last_seen": "",
                        }
                    evt_line = f"{g_name}：{g_msg[:70]}"
                    if evt_line not in guests_by_key[target_key]["events"]:
                        guests_by_key[target_key]["events"].append(evt_line)

        # 3. Also include any non-master entries in favorability.json that aren't already represented
        notify_ch = str(base_data.get("notify_channel_id", "")).strip() if isinstance(base_data, dict) else ""
        bot_state = cls.read_json(BOT_STATE_FILE, {})
        last_ch = str(bot_state.get("last_active_channel_id", "")).strip() if isinstance(bot_state, dict) else ""
        for fk, fscore in favor_data.items():
            fk_str = str(fk).strip()
            if not fk_str or fk_str == master_id or fk_str in ("主人", "master"):
                continue
            # If fk_str is the master's main notify channel and no guests spoke there, skip if already covered
            already_has = any(g["key"] == fk_str or g["user_id"] == fk_str for g in guests_by_key.values())
            if already_has:
                continue
            if fk_str in (notify_ch, last_ch) and master_id:
                # Master's primary channel ID in channel-keyed favorability; skip unless explicitly in guest_notes
                continue
            try:
                sc = int(fscore)
            except Exception:
                sc = 50
            guests_by_key[fk_str] = {
                "key": fk_str,
                "user_id": fk_str,
                "name": f"訪客 ({fk_str[-6:]})" if fk_str.isdigit() and len(fk_str) > 8 else fk_str,
                "favorability": sc,
                "relationship": _tier_from_score(sc),
                "events": [],
                "cognition_note": "尚無特定認知筆記（可點擊右側直接新增）",
                "last_seen": "",
            }

        return list(guests_by_key.values())

    @classmethod
    def save_guest_relationships(cls, guests: List[Dict[str, Any]]) -> None:
        out_notes: Dict[str, Any] = {}
        favor_data = cls.read_json(FAVORABILITY_FILE, {})
        if not isinstance(favor_data, dict):
            favor_data = {}
        for g in guests:
            key = str(g.get("key") or g.get("user_id") or g.get("name", "")).strip()
            if not key:
                continue
            uid = str(g.get("user_id", key)).strip()
            fav = int(g.get("favorability", 50))
            out_notes[key] = {
                "user_id": uid,
                "name": str(g.get("name", key)).strip(),
                "favorability": fav,
                "relationship": str(g.get("relationship", "一般訪客")).strip(),
                "events": [str(x).strip() for x in g.get("events", []) if str(x).strip()],
                "cognition_note": str(g.get("cognition_note", "")).strip(),
                "last_seen": str(g.get("last_seen", "")),
            }
            if uid:
                favor_data[uid] = fav
        cls.write_json(GUEST_NOTES_FILE, out_notes)
        cls.write_json(FAVORABILITY_FILE, favor_data)

    @classmethod
    def generate_default_memory_filename(cls, target_dir: Path | str = ROOT) -> str:
        """
        Generates default memory bundle filename in the format:
        `{當前角色名}-Backup-{yymmdd}-{流水號}.memory`
        """
        persona = cls.load_active_persona()
        raw_name = str(persona.get("name", "黛安娜")).strip() or "黛安娜"
        safe_name = re.sub(r'[<>:"/\\|?*]+', "_", raw_name)
        yymmdd = datetime.now().strftime("%y%m%d")
        prefix = f"{safe_name}-Backup-{yymmdd}-"

        max_seq = 0
        seq_pattern = re.compile(re.escape(prefix) + r"(\d+)\.memory$", re.IGNORECASE)
        for scan_dir in {Path(target_dir) if target_dir else ROOT, ROOT, DATA}:
            if not scan_dir.exists():
                continue
            for f in scan_dir.glob(f"{prefix}*.memory"):
                m = seq_pattern.match(f.name)
                if m:
                    try:
                        max_seq = max(max_seq, int(m.group(1)))
                    except Exception:
                        pass
        next_seq = max_seq + 1
        return f"{prefix}{next_seq:02d}.memory"

    @staticmethod
    def blank_persona_template() -> Dict[str, Any]:
        return {
            "name": "",
            "age_style": "",
            "background": "",
            "relationship": "",
            "guest_attitude": "",
            "core_traits": [""],
            "speech_patterns": [""],
            "tone_rules": [""],
            "evolvable_persona": {
                "learned_traits": [""],
                "favorability_reactions": {"low": "", "medium": "", "high": ""},
                "few_shot_examples": [{"user": "", "diana": ""}],
            },
            "personality_traits": [""],
            "few_shot_examples": [{"user": "", "diana": ""}],
            "favorability_reactions": {"low": "", "medium": "", "high": ""},
        }

    @classmethod
    def collect_telemetry_snapshot(cls) -> Dict[str, Any]:
        """Reads non-blocking telemetry summary from `data/` and `config/` for dashboard cards."""
        version_data = cls.read_json(VERSION_FILE, {"bot": "未知", "character_persona": "未知"})
        favor_data = cls.read_json(FAVORABILITY_FILE, {})
        reminders_data = cls.read_json(REMINDERS_FILE, [])
        action_imgs = cls.read_json(ACTION_IMAGES, [])
        persona_data = cls.read_json(PERSONA, {})
        bot_state = cls.read_json(BOT_STATE_FILE, {})
        runtime_status = cls.read_json(STATUS_FILE, {}) if STATUS_FILE.exists() else {}

        max_favor = max(favor_data.values()) if isinstance(favor_data, dict) and favor_data else 0
        user_count = len(favor_data) if isinstance(favor_data, dict) else 0
        reminder_count = len(reminders_data) if isinstance(reminders_data, list) else 0
        action_img_count = len(action_imgs) if isinstance(action_imgs, list) else 0

        guests = cls.collect_guest_relationships()
        guest_count = len(guests)
        guest_note_count = sum(1 for g in guests if g.get("cognition_note") or g.get("events"))

        next_rem_hint = "目前無排程"
        if isinstance(reminders_data, list) and reminders_data:
            first_r = reminders_data[0]
            if isinstance(first_r, dict):
                t_str = str(first_r.get("time", first_r.get("datetime", ""))).strip()
                c_str = str(first_r.get("content", first_r.get("task", ""))).strip()
                next_rem_hint = f"{t_str[-11:] if len(t_str) > 11 else t_str} {c_str[:8]}".strip() or "點擊查看排程"
            else:
                next_rem_hint = str(first_r)[:14]

        persona_files_count = len(list(CONFIG.glob("*.persona")))

        return {
            "bot_version": version_data.get("bot", "—"),
            "persona_version": version_data.get("character_persona", "—"),
            "persona_name": persona_data.get("name", "未設定") if isinstance(persona_data, dict) else "未設定",
            "persona_age_style": persona_data.get("age_style", "") if isinstance(persona_data, dict) else "",
            "persona_files_count": persona_files_count,
            "user_count": user_count,
            "guest_count": guest_count,
            "guest_note_count": guest_note_count,
            "max_favor": max_favor,
            "reminder_count": reminder_count,
            "next_reminder_hint": next_rem_hint,
            "action_img_count": action_img_count,
            "last_message_time": bot_state.get("last_message_time", "無紀錄"),
            "runtime": runtime_status,
        }

    # --- Memory Inspection, Editing, Export & Import Helpers ---
    @classmethod
    def read_chroma_memories(cls) -> List[Tuple[str, str]]:
        if not CHROMA_SQLITE_FILE.exists():
            return []
        try:
            conn = sqlite3.connect(str(CHROMA_SQLITE_FILE), timeout=5.0)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT e.embedding_id, f.c0 "
                    "FROM embeddings e "
                    "JOIN embedding_fulltext_search_content f ON e.id = f.id "
                    "ORDER BY e.id DESC"
                )
                rows = cur.fetchall()
                return [(str(r[0]), str(r[1] or "")) for r in rows]
            finally:
                conn.close()
        except Exception:
            return []

    @classmethod
    def update_chroma_memory_text(cls, embedding_id: str, new_text: str) -> bool:
        if not CHROMA_SQLITE_FILE.exists() or not embedding_id:
            return False
        conn = sqlite3.connect(str(CHROMA_SQLITE_FILE), timeout=8.0)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM embeddings WHERE embedding_id = ?", (embedding_id,))
            row = cur.fetchone()
            if not row:
                return False
            internal_id = row[0]
            cur.execute("UPDATE embedding_fulltext_search_content SET c0 = ? WHERE id = ?", (new_text, internal_id))
            cur.execute(
                "UPDATE embedding_metadata SET string_value = ? WHERE id = ? AND key = 'chroma:document'",
                (new_text, internal_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    @classmethod
    def delete_chroma_memory(cls, embedding_id: str) -> bool:
        if not CHROMA_SQLITE_FILE.exists() or not embedding_id:
            return False
        conn = sqlite3.connect(str(CHROMA_SQLITE_FILE), timeout=8.0)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM embeddings WHERE embedding_id = ?", (embedding_id,))
            row = cur.fetchone()
            if not row:
                return False
            internal_id = row[0]
            cur.execute("DELETE FROM embedding_fulltext_search_content WHERE id = ?", (internal_id,))
            cur.execute("DELETE FROM embedding_metadata WHERE id = ?", (internal_id,))
            cur.execute("DELETE FROM embeddings WHERE id = ?", (internal_id,))
            conn.commit()
            return True
        finally:
            conn.close()

    @classmethod
    def export_memory_bundle(cls, dest_zip_path: Path | str) -> int:
        dest_zip_path = Path(dest_zip_path)
        dest_zip_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        mem_files = [
            USER_PROFILE_FILE,
            MEMORY_FILE,
            FAVORABILITY_FILE,
            GUEST_NOTES_FILE,
            REMINDERS_FILE,
            BASE_FILE,
            BOT_STATE_FILE,
        ]
        with zipfile.ZipFile(dest_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for mf in mem_files:
                if mf.exists():
                    zf.write(mf, f"data/{mf.name}")
                    count += 1

            # Also export evolvable_persona from character_persona.json so learned traits are preserved
            persona_obj = cls.read_json(PERSONA, {})
            if isinstance(persona_obj, dict) and "evolvable_persona" in persona_obj:
                zf.writestr(
                    "config/evolvable_persona.json",
                    json.dumps(persona_obj["evolvable_persona"], ensure_ascii=False, indent=2),
                )
                count += 1

            # Export ChromaDB vector database directory
            if CHROMA_DB_DIR.exists():
                for root_dir, _dirs, files in os.walk(CHROMA_DB_DIR):
                    for fn in files:
                        full_p = Path(root_dir) / fn
                        rel_p = full_p.relative_to(ROOT).as_posix()
                        zf.write(full_p, rel_p)
                        count += 1

            manifest = {
                "bundle_format": "diana-memory-bundle-v1",
                "exported_at": datetime.now().isoformat(),
                "file_count": count,
            }
            zf.writestr("memory_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            count += 1
        return count

    @classmethod
    def import_memory_bundle(cls, src_zip_path: Path | str) -> int:
        src_zip_path = Path(src_zip_path)
        if not src_zip_path.exists():
            raise FileNotFoundError(f"找不到記憶備份包：{src_zip_path}")
        restored = 0
        DATA.mkdir(parents=True, exist_ok=True)
        CONFIG.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(src_zip_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                norm = info.filename.replace("\\", "/").lstrip("/")
                if norm == "memory_manifest.json":
                    continue
                if norm == "config/evolvable_persona.json":
                    raw_ev = zf.read(info).decode("utf-8", errors="replace")
                    ev_obj = json.loads(raw_ev)
                    if isinstance(ev_obj, dict):
                        active_p = cls.load_active_persona()
                        active_p["evolvable_persona"] = ev_obj
                        # Merge learned traits with core traits
                        core_t = active_p.get("core_traits", [])
                        learned_t = ev_obj.get("learned_traits", [])
                        if isinstance(core_t, list) and isinstance(learned_t, list):
                            combined = []
                            for t in core_t + learned_t:
                                if t and t not in combined:
                                    combined.append(t)
                            active_p["personality_traits"] = combined
                        if isinstance(ev_obj.get("favorability_reactions"), dict):
                            active_p["favorability_reactions"] = ev_obj["favorability_reactions"]
                        if isinstance(ev_obj.get("few_shot_examples"), list):
                            active_p["few_shot_examples"] = ev_obj["few_shot_examples"]
                        cls.write_json(PERSONA, active_p)
                        restored += 1
                    continue

                if norm.startswith("data/") or norm.startswith("diana_memory_db/"):
                    dest_p = (ROOT / norm).resolve()
                    if not str(dest_p).startswith(str(ROOT.resolve())):
                        continue
                    dest_p.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src_s, open(dest_p, "wb") as out_s:
                        shutil.copyfileobj(src_s, out_s)
                    restored += 1
        return restored


# ---------------------------------------------------------------------------
# 3. Execution / Worker Layer (Decoupled from UI Widgets)
# ---------------------------------------------------------------------------
class ExecutionWorkers:
    """
    Independent worker routines that execute background I/O or subprocesses
    and communicate strictly via `signal_queue` broadcasts.
    """

    def __init__(self, signal_queue: "queue.Queue[BroadcastSignal]", state: RuntimeLifecycleState) -> None:
        self.signal_queue = signal_queue
        self.state = state
        self.bot_proc: Optional[subprocess.Popen[str]] = None

    def emit(self, signal_type: str, **payload: Any) -> None:
        self.signal_queue.put(BroadcastSignal(signal_type=signal_type, payload=payload))

    @staticmethod
    def _find_python_312() -> Tuple[Optional[str], Optional[str]]:
        if not sys.platform.startswith("win"):
            return None, None
        try:
            result = subprocess.run(
                ["py", "-3.12", "-c", "import sys; print(sys.executable)"],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except FileNotFoundError:
            return None, None
        except subprocess.TimeoutExpired:
            return None, "偵測 Python 3.12 逾時，請確認 Python Launcher 狀態。"

        if result.returncode != 0:
            return None, None
        executable = result.stdout.strip()
        if executable and Path(executable).is_file():
            return executable, None
        return None, "Python Launcher 回傳的 Python 3.12 路徑無效。"

    # --- Bot Process Lifecycle ---
    def is_bot_alive(self) -> bool:
        alive = bool(self.bot_proc and self.bot_proc.poll() is None)
        self.state.is_bot_running_flag = alive
        return alive

    def start_bot_process(self) -> bool:
        if self.is_bot_alive():
            return False
        self.state.is_bot_running_flag = True
        self.emit(
            "LOG_APPEND",
            text=f"\n[{datetime.now().strftime('%H:%M:%S')}] === 🚀 正在啟動 Diana Bot (bot.py) ===\n",
            tag="system",
        )
        self.bot_proc = subprocess.Popen(
            [sys.executable, "-u", "bot.py"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._pump_bot_stdout, args=(self.bot_proc,), daemon=True).start()
        return True

    def _pump_bot_stdout(self, proc: subprocess.Popen[str]) -> None:
        try:
            if proc.stdout:
                for line in proc.stdout:
                    self.emit("LOG_APPEND", text=line)
        finally:
            rc = proc.poll()
            if proc is self.bot_proc:
                self.state.is_bot_running_flag = False
            self.emit(
                "LOG_APPEND",
                text=f"[{datetime.now().strftime('%H:%M:%S')}] === ⏹ Diana 行程已結束 (exit code: {rc}) ===\n",
                tag="system",
            )
            self.emit("BOT_PROCESS_EXITED", returncode=rc)

    def stop_bot_process(self) -> bool:
        if not self.is_bot_alive() or not self.bot_proc:
            self.state.is_bot_running_flag = False
            return False
        try:
            self.bot_proc.terminate()
            self.emit(
                "LOG_APPEND",
                text=f"[{datetime.now().strftime('%H:%M:%S')}] === 🛑 已發送停止訊號予 Diana ===\n",
                tag="warn",
            )
            return True
        except Exception as e:
            self.emit("LOG_APPEND", text=f"❌ 停止程序時發生異常: {e}\n", tag="error")
            return False

    # --- Patch Pusher Companion Widget Launcher ---
    def launch_patch_pusher(self) -> bool:
        if PATCH_PUSHER_SCRIPT.exists():
            exe_dir = Path(sys.executable).resolve().parent
            pythonw = exe_dir / "pythonw.exe"
            runner = str(pythonw) if (sys.platform.startswith("win") and pythonw.exists()) else sys.executable
            subprocess.Popen([runner, str(PATCH_PUSHER_SCRIPT)], cwd=str(ROOT))
            return True
        if PATCH_PUSHER_EXE.exists():
            subprocess.Popen([str(PATCH_PUSHER_EXE)], cwd=str(ROOT))
            return True
        return False

    # --- Cold-Install & .seed Packager Companion Launcher ---
    def _ensure_seed_installer_exe(self) -> bool:
        if SEED_INSTALLER_EXE.exists():
            return True
        if SEED_INSTALLER_BUILD_BAT.exists() and sys.platform.startswith("win"):
            try:
                res = subprocess.run(
                    ["cmd", "/c", str(SEED_INSTALLER_BUILD_BAT)],
                    cwd=str(ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    check=False,
                )
                return res.returncode == 0 and SEED_INSTALLER_EXE.exists()
            except Exception:
                return False
        return False

    def launch_seed_installer(self) -> bool:
        if not self._ensure_seed_installer_exe():
            return False
        subprocess.Popen([str(SEED_INSTALLER_EXE)], cwd=str(ROOT))
        return True

    def pack_clean_seed_async(self, output_seed_path: Optional[Path] = None) -> None:
        target_seed = output_seed_path or DEFAULT_SEED_FILE
        self.emit("SEED_PACK_STARTED", path=str(target_seed))

        def _worker() -> None:
            try:
                self.emit(
                    "LOG_APPEND",
                    text=f"\n[{datetime.now().strftime('%H:%M:%S')}] === 🌱 開始封裝無記憶 .seed 系統包 ({target_seed.name}) ===\n",
                    tag="system",
                )
                if not self._ensure_seed_installer_exe():
                    raise RuntimeError("找不到或無法編譯 DianaSeedInstaller.exe，請確認 installer/DianaSeedInstaller.cs 是否存在。")
                proc = subprocess.Popen(
                    [str(SEED_INSTALLER_EXE), "--pack-seed", str(ROOT), str(target_seed)],
                    cwd=str(ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if proc.stdout:
                    for line in proc.stdout:
                        self.emit("LOG_APPEND", text=line)
                proc.wait()
                self.emit("SEED_PACK_FINISHED", returncode=proc.returncode, path=str(target_seed))
            except Exception as e:
                self.emit("LOG_APPEND", text=f"❌ 封裝 .seed 失敗: {e}\n", tag="error")
                self.emit("SEED_PACK_FINISHED", returncode=-1, error=str(e), path=str(target_seed))

        threading.Thread(target=_worker, daemon=True).start()

    # --- API Health Check Worker ---
    def test_openai_endpoint_async(self, target_id: str, base_url: str, api_key: str, label: str) -> None:
        flag_attr = f"is_testing_{target_id}_api_flag"
        if getattr(self.state, flag_attr, False):
            return
        setattr(self.state, flag_attr, True)
        self.emit("API_TEST_STATUS", target_id=target_id, status="testing", message=f"正在測試 {label}...")

        def _worker() -> None:
            t0 = time.perf_counter()
            try:
                clean_base = base_url.strip().rstrip("/")
                if not clean_base:
                    raise ValueError("尚未填寫 Base URL")
                url = clean_base + "/models"
                headers = {
                    "Authorization": f"Bearer {api_key.strip() or 'lm-studio'}",
                    "User-Agent": "Diana-Control-Center/2.1",
                }
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                latency_ms = int((time.perf_counter() - t0) * 1000)
                model_count = 0
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed.get("data"), list):
                        model_count = len(parsed["data"])
                except Exception:
                    pass
                detail = f"連線正常 ({latency_ms} ms" + (f", 偵測到 {model_count} 個模型)" if model_count else ")")
                self.emit("API_TEST_STATUS", target_id=target_id, status="ok", message=detail, label=label)
            except Exception as e:
                self.emit("API_TEST_STATUS", target_id=target_id, status="error", message=f"連線失敗：{e}", label=label)
            finally:
                setattr(self.state, flag_attr, False)

        threading.Thread(target=_worker, daemon=True).start()

    # --- Persona Generation Worker ---
    def generate_personas_async(
        self,
        description: str,
        use_web_api: bool,
        local_base_url: str,
        local_model: str,
        web_base_url: str,
        web_model: str,
        chat_key: str,
    ) -> None:
        if self.state.is_generating_persona_flag:
            return
        self.state.is_generating_persona_flag = True
        self.emit("PERSONA_GEN_STARTED")

        def _worker() -> None:
            schema = (
                "name, age_style, background, relationship, guest_attitude, "
                "personality_traits(array of strings), speech_patterns(array of strings), "
                "tone_rules(array of strings), few_shot_examples(array of {user,diana}), "
                "favorability_reactions({low,medium,high})"
            )
            prompt = (
                f"請根據以下描述，設計三個風格與切入點明顯不同、但都完整可直接使用的 AI 角色 Persona。\n"
                f"角色需求描述：{description}\n"
                f"每個角色物件必須包含以下 JSON 欄位：{schema}\n"
                f"請只輸出純 JSON Array（恰好包含 3 個 JSON object），不要包含 Markdown 語法或額外解釋。"
            )
            try:
                if use_web_api:
                    base = web_base_url.strip().rstrip("/")
                    model = web_model.strip()
                    token = chat_key.strip()
                    if not token:
                        raise ValueError("使用 Web API 生成需要先填寫 Chat API Key")
                else:
                    base = local_base_url.strip().rstrip("/")
                    model = local_model.strip()
                    token = "lm-studio"

                url = base + "/chat/completions"
                payload = json.dumps(
                    {
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.88,
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "Diana-Control-Center/2.1",
                    },
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                text = data["choices"][0]["message"]["content"].strip()
                if "</think>" in text:
                    text = text.split("</think>", 1)[-1].strip()
                if text.startswith("```"):
                    lines = text.splitlines()
                    if lines and lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].strip().startswith("```"):
                        lines = lines[:-1]
                    text = "\n".join(lines).strip()

                candidates = json.loads(text)
                if not isinstance(candidates, list) or len(candidates) < 1:
                    raise ValueError("LLM 回傳格式並非有效的角色陣列 (JSON Array)。")
                self.emit("PERSONA_GEN_SUCCESS", candidates=candidates[:3])
            except Exception as e:
                self.emit("PERSONA_GEN_ERROR", error=str(e))
            finally:
                self.state.is_generating_persona_flag = False

        threading.Thread(target=_worker, daemon=True).start()

    # --- Pip Dependency Installer Worker ---
    def install_dependencies_async(self) -> None:
        if self.state.is_installing_deps_flag:
            return
        self.state.is_installing_deps_flag = True
        self.emit("DEPS_INSTALL_STARTED")

        def _worker() -> None:
            try:
                self.emit(
                    "LOG_APPEND",
                    text=(
                        f"\n[{datetime.now().strftime('%H:%M:%S')}] === 📦 開始安裝 requirements.txt 依賴套件 ===\n"
                        f"使用 Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}: "
                        f"{sys.executable}\n"
                    ),
                    tag="system",
                )
                proc = subprocess.Popen(
                    [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if proc.stdout:
                    for line in proc.stdout:
                        self.emit("LOG_APPEND", text=line)
                proc.wait()
                python312_path = None
                python312_detection_error = None
                if proc.returncode != 0 and sys.version_info[:2] != (3, 12):
                    python312_path, python312_detection_error = self._find_python_312()
                    if python312_detection_error:
                        self.emit(
                            "LOG_APPEND",
                            text=f"⚠ {python312_detection_error}\n",
                            tag="warn",
                        )
                self.emit(
                    "DEPS_INSTALL_FINISHED",
                    returncode=proc.returncode,
                    python312_path=python312_path,
                )
            except Exception as e:
                self.emit("LOG_APPEND", text=f"❌ 套件安裝失敗: {e}\n", tag="error")
                self.emit("DEPS_INSTALL_FINISHED", returncode=-1, error=str(e))
            finally:
                self.state.is_installing_deps_flag = False

        threading.Thread(target=_worker, daemon=True).start()

    # --- Manual Evolution Trigger Worker ---
    def run_evolution_now_async(self) -> None:
        if self.state.is_running_evolution_flag:
            return
        self.state.is_running_evolution_flag = True
        self.emit("EVOLUTION_RUN_STARTED")

        def _worker() -> None:
            try:
                self.emit(
                    "LOG_APPEND",
                    text=f"\n[{datetime.now().strftime('%H:%M:%S')}] === 🧬 手動觸發 Diana Evolution (--run-now) ===\n",
                    tag="system",
                )
                proc = subprocess.Popen(
                    [sys.executable, "-u", "diana_evolution.py", "--run-now"],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if proc.stdout:
                    for line in proc.stdout:
                        self.emit("LOG_APPEND", text=line)
                proc.wait()
                self.emit("EVOLUTION_RUN_FINISHED", returncode=proc.returncode)
            except Exception as e:
                self.emit("LOG_APPEND", text=f"❌ 演化腳本執行失敗: {e}\n", tag="error")
                self.emit("EVOLUTION_RUN_FINISHED", returncode=-1, error=str(e))
            finally:
                self.state.is_running_evolution_flag = False

        threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# 4. Custom Responsive UI Widgets (Scroll Container, Stepper Canvas, Buttons)
# ---------------------------------------------------------------------------
class ResponsiveScrollFrame(tk.Frame):
    """
    Adaptive scrollable viewport container that automatically stretches its
    inner content frame to match the canvas width while allowing smooth vertical
    mousewheel scrolling when content height exceeds the window height.
    """

    def __init__(self, parent: tk.Widget, bg: str = PALETTE["bg_app"], **kwargs: Any) -> None:
        super().__init__(parent, bg=bg, **kwargs)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.v_scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.v_scroll.set)

        self.canvas.pack(side="left", fill="both", expand=True)

        self.content = tk.Frame(self.canvas, bg=bg)
        self._win_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")

        self._scrollbar_visible = False
        self.content.bind("<Configure>", self._on_content_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _on_content_configure(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        self._update_scrollregion_and_bar()

    def _on_canvas_configure(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if event.width > 40:
            self.canvas.itemconfigure(self._win_id, width=event.width)
        self._update_scrollregion_and_bar()

    def _update_scrollregion_and_bar(self) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        req_h = self.content.winfo_reqheight()
        vis_h = max(self.canvas.winfo_height(), 1)
        need_bar = req_h > vis_h + 6
        if need_bar and not self._scrollbar_visible:
            self.v_scroll.pack(side="right", fill="y")
            self._scrollbar_visible = True
        elif not need_bar and self._scrollbar_visible:
            self.v_scroll.pack_forget()
            self._scrollbar_visible = False

    def _bind_wheel(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_mousewheel)
        self.canvas.bind_all("<Button-5>", self._on_mousewheel)

    def _unbind_wheel(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if not self._scrollbar_visible:
            return
        if getattr(event, "num", None) == 4:
            self.canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            self.canvas.yview_scroll(1, "units")
        elif getattr(event, "delta", 0):
            step = -1 if event.delta > 0 else 1
            self.canvas.yview_scroll(step, "units")


class PipelineStepperCanvas(tk.Canvas):
    """
    Fully responsive vector pipeline stepper visualizing Diana's 6 runtime stages:
    等待待命 -> 訊息分析 -> 記憶檢索 -> LLM 思考 -> 訊號回覆 -> 整理記憶
    Dynamically scales margins, node radii, and font sizes to fit any width without clipping.
    """

    def __init__(self, parent: tk.Widget, **kwargs: Any) -> None:
        super().__init__(
            parent,
            height=92,
            bg=PALETTE["bg_card"],
            highlightthickness=0,
            bd=0,
            **kwargs,
        )
        self.active_stage: str = ""
        self.is_online: bool = False
        self.pulse_phase: int = 0
        self._canvas_w: int = 760
        self._canvas_h: int = 92
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if event.width > 120:
            self._canvas_w = event.width
        if event.height > 50:
            self._canvas_h = event.height
        self.redraw()

    def set_state(self, stage: str, is_online: bool, advance_pulse: bool = False) -> None:
        changed = (stage != self.active_stage) or (is_online != self.is_online)
        self.active_stage = stage
        self.is_online = is_online
        if advance_pulse and is_online:
            self.pulse_phase = (self.pulse_phase + 1) % 2
            changed = True
        if changed:
            self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w = max(self._canvas_w, 360)
        h = max(self._canvas_h, 76)
        n = len(PIPELINE_STAGES)

        is_compact = w < 640
        pad_x = max(36, min(64, int(w * 0.075)))
        usable_w = max(w - pad_x * 2, 220)
        step_w = usable_w / max(n - 1, 1)
        cy = int(h * 0.36)

        node_r_active = 11 if is_compact else 13
        node_r_norm = 9 if is_compact else 11
        title_font_sz = 9 if is_compact else 10
        sub_font_sz = 7 if is_compact else 8

        stage_keys = [s[0] for s in PIPELINE_STAGES]
        active_idx = stage_keys.index(self.active_stage) if self.active_stage in stage_keys else -1

        # Draw connecting track lines first
        for i in range(n - 1):
            x1 = pad_x + i * step_w
            x2 = pad_x + (i + 1) * step_w
            is_passed = self.is_online and active_idx > i
            line_color = PALETTE["accent"] if is_passed else "#26334D"
            self.create_line(x1, cy, x2, cy, fill=line_color, width=3)

        # Draw nodes and labels
        for i, (key, title_zh, subtitle_en) in enumerate(PIPELINE_STAGES):
            cx = pad_x + i * step_w
            is_current = self.is_online and (key == self.active_stage)
            is_passed = self.is_online and (active_idx > i)

            if is_current:
                node_color = PALETTE["success"] if key == "idle" else PALETTE["accent"]
                halo_r = (node_r_active + 7) if self.pulse_phase == 0 else (node_r_active + 10)
                self.create_oval(
                    cx - halo_r,
                    cy - halo_r,
                    cx + halo_r,
                    cy + halo_r,
                    fill=PALETTE["accent_soft"] if key != "idle" else PALETTE["success_bg"],
                    outline=node_color,
                    width=1,
                )
                self.create_oval(
                    cx - node_r_active,
                    cy - node_r_active,
                    cx + node_r_active,
                    cy + node_r_active,
                    fill=node_color,
                    outline="#FFFFFF",
                    width=2,
                )
                self.create_text(cx, cy, text="●", fill="#FFFFFF", font=("Segoe UI", 8, "bold"))
                label_color = "#FFFFFF"
                sub_color = PALETTE["cyan"] if key != "idle" else PALETTE["success"]
            elif is_passed:
                self.create_oval(
                    cx - node_r_norm,
                    cy - node_r_norm,
                    cx + node_r_norm,
                    cy + node_r_norm,
                    fill=PALETTE["accent_soft"],
                    outline=PALETTE["accent"],
                    width=2,
                )
                self.create_text(cx, cy, text="✓", fill=PALETTE["accent"], font=("Segoe UI", 8, "bold"))
                label_color = PALETTE["text_primary"]
                sub_color = PALETTE["text_secondary"]
            else:
                self.create_oval(
                    cx - node_r_norm,
                    cy - node_r_norm,
                    cx + node_r_norm,
                    cy + node_r_norm,
                    fill=PALETTE["bg_input"],
                    outline="#334155",
                    width=2,
                )
                self.create_text(cx, cy, text=str(i + 1), fill=PALETTE["text_muted"], font=("Segoe UI", 8, "bold"))
                label_color = PALETTE["text_secondary"] if self.is_online else PALETTE["text_muted"]
                sub_color = PALETTE["text_muted"]

            self.create_text(
                cx,
                cy + node_r_active + 14,
                text=title_zh,
                fill=label_color,
                font=("Microsoft JhengHei UI", title_font_sz, "bold" if is_current else "normal"),
            )
            self.create_text(
                cx,
                cy + node_r_active + 30,
                text=subtitle_en,
                fill=sub_color,
                font=("Segoe UI", sub_font_sz),
            )


class ModernButton(tk.Frame):
    """Flat, crisp modern button with hover state, custom cursor, and disabled state."""

    def __init__(
        self,
        parent: tk.Widget,
        text: str,
        command: Callable[[], None],
        variant: str = "secondary",
        padx: int = 14,
        pady: int = 7,
    ) -> None:
        super().__init__(parent, bg=parent.cget("bg"))
        self.command = command
        self.variant = variant
        self._enabled = True

        styles = {
            "primary": (PALETTE["accent"], PALETTE["accent_hover"], "#FFFFFF", PALETTE["accent"]),
            "success": (PALETTE["success"], "#059669", "#FFFFFF", PALETTE["success"]),
            "danger": ("#DC2626", "#B91C1C", "#FFFFFF", "#DC2626"),
            "secondary": (PALETTE["bg_card_alt"], "#334155", PALETTE["text_primary"], PALETTE["border"]),
            "ghost": (PALETTE["bg_card"], PALETTE["bg_card_alt"], PALETTE["text_secondary"], PALETTE["border"]),
        }
        bg_norm, bg_hover, fg_color, border_color = styles.get(variant, styles["secondary"])
        self.bg_norm = bg_norm
        self.bg_hover = bg_hover
        self.fg_color = fg_color

        self.configure(highlightthickness=1, highlightbackground=border_color)
        self.lbl = tk.Label(
            self,
            text=text,
            bg=bg_norm,
            fg=fg_color,
            font=("Microsoft JhengHei UI", 9, "bold" if variant in ("primary", "success", "danger") else "normal"),
            padx=padx,
            pady=pady,
            cursor="hand2",
        )
        self.lbl.pack(fill="both", expand=True)

        for w in (self, self.lbl):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            w.bind("<Button-1>", self._on_click)

    def _on_enter(self, _e: Any) -> None:
        if self._enabled:
            self.lbl.configure(bg=self.bg_hover)

    def _on_leave(self, _e: Any) -> None:
        if self._enabled:
            self.lbl.configure(bg=self.bg_norm)

    def _on_click(self, _e: Any) -> None:
        if self._enabled and self.command:
            self.command()

    def set_enabled(self, enabled: bool, text: Optional[str] = None) -> None:
        self._enabled = enabled
        if text is not None:
            self.lbl.configure(text=text)
        if enabled:
            self.lbl.configure(bg=self.bg_norm, fg=self.fg_color, cursor="hand2")
        else:
            self.lbl.configure(bg="#1E293B", fg=PALETTE["text_muted"], cursor="arrow")


class ModeSliderSwitch(tk.Frame):
    """
    Interactive slider toggle switch for switching Persona Studio between
    'visual' (📝 表單模式) and 'json' ({ } 原始碼模式).
    """

    def __init__(
        self,
        parent: tk.Widget,
        on_switch: Callable[[str], None],
        initial_mode: str = "visual",
    ) -> None:
        super().__init__(
            parent,
            bg=PALETTE["bg_input"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=8,
            pady=4,
            cursor="hand2",
        )
        self.on_switch = on_switch
        self.mode = initial_mode

        self.lbl_left = tk.Label(
            self,
            text="📝 表單模式",
            bg=PALETTE["bg_input"],
            fg="#FFFFFF",
            font=("Microsoft JhengHei UI", 9, "bold"),
            cursor="hand2",
        )
        self.lbl_left.pack(side="left", padx=(2, 6))

        self.canvas = tk.Canvas(
            self,
            width=48,
            height=22,
            bg=PALETTE["bg_input"],
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.canvas.pack(side="left", padx=2)

        self.lbl_right = tk.Label(
            self,
            text="{ } 原始碼模式",
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_muted"],
            font=("Microsoft JhengHei UI", 9),
            cursor="hand2",
        )
        self.lbl_right.pack(side="left", padx=(6, 2))

        self.lbl_left.bind("<Button-1>", lambda _e: self._trigger("visual"))
        self.lbl_right.bind("<Button-1>", lambda _e: self._trigger("json"))
        self.canvas.bind("<Button-1>", lambda _e: self._trigger("json" if self.mode == "visual" else "visual"))
        self.bind("<Button-1>", lambda _e: self._trigger("json" if self.mode == "visual" else "visual"))

        self._redraw()

    def _trigger(self, target_mode: str) -> None:
        if self.on_switch:
            self.on_switch(target_mode)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._redraw()

    def _redraw(self) -> None:
        self.canvas.delete("all")
        is_json = self.mode == "json"
        track_color = PALETTE["cyan"] if is_json else PALETTE["accent"]
        # Draw rounded pill track (left cap + rect + right cap)
        self.canvas.create_oval(2, 2, 20, 20, fill=track_color, outline=track_color)
        self.canvas.create_oval(28, 2, 46, 20, fill=track_color, outline=track_color)
        self.canvas.create_rectangle(11, 2, 37, 20, fill=track_color, outline=track_color)
        # Draw circular knob
        knob_cx = 37 if is_json else 11
        self.canvas.create_oval(knob_cx - 7, 4, knob_cx + 7, 18, fill="#FFFFFF", outline="#E2E8F0")

        if is_json:
            self.lbl_left.configure(fg=PALETTE["text_muted"], font=("Microsoft JhengHei UI", 9))
            self.lbl_right.configure(fg=PALETTE["cyan"], font=("Microsoft JhengHei UI", 9, "bold"))
        else:
            self.lbl_left.configure(fg="#FFFFFF", font=("Microsoft JhengHei UI", 9, "bold"))
            self.lbl_right.configure(fg=PALETTE["text_muted"], font=("Microsoft JhengHei UI", 9))


# ---------------------------------------------------------------------------
# 5. Main Adaptive Presentation Layer (DianaControlCenterApp)
# ---------------------------------------------------------------------------
class DianaControlCenterApp(tk.Tk):
    """
    Modernized, fully responsive Tkinter desktop application for Diana Bot Control Center.
    Adapts seamlessly to window resizing, screen resolution, and High-DPI scaling.
    """

    def __init__(self) -> None:
        enable_high_dpi()
        super().__init__()
        os.chdir(ROOT)
        ConfigRepository.ensure_dirs()

        # Core architectural state, viewport controller & worker bus
        self.signal_queue: "queue.Queue[BroadcastSignal]" = queue.Queue()
        self.lifecycle = RuntimeLifecycleState()
        self.viewport = ViewportLayoutState()
        self.workers = ExecutionWorkers(self.signal_queue, self.lifecycle)
        self.cfg: Dict[str, Any] = ConfigRepository.load_gui_config()

        # Screen-adaptive initial window setup
        self.title("Diana Bot Control Center — 智慧自適應中控台")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        init_w, init_h, pos_x, pos_y = ViewportBreakpointController.compute_initial_geometry(sw, sh)
        self.viewport, _ = ViewportBreakpointController.evaluate_viewport(init_w, init_h, self.viewport)
        self.geometry(f"{init_w}x{init_h}+{pos_x}+{pos_y}")
        self.minsize(780, 500)
        self.resizable(True, True)
        self.configure(bg=PALETTE["bg_app"])

        # Tkinter reactive variables
        self.vars: Dict[str, tk.StringVar] = {
            k: tk.StringVar()
            for k in [
                "local_base_url",
                "local_model",
                "web_base_url",
                "web_model",
                "master_backend",
                "master_model",
                "guest_backend",
                "guest_model",
                "image_endpoint",
                "image_model",
                "discord",
                "chat_key",
                "evolution_key",
                "evolution_model",
                "image_key",
                "temperature",
                "thinking_tokens",
            ]
        }
        self.log_filter_var = tk.StringVar()
        self.auto_scroll_var = tk.BooleanVar(value=True)
        self.persona_prompt_var = tk.StringVar()
        self.persona_gen_backend_var = tk.StringVar(value="local")
        self._raw_log_buffer: List[Tuple[str, str]] = []
        self._toast_after_id: Optional[str] = None
        self._resize_debounce_id: Optional[str] = None

        self._configure_ttk_styles()
        self._build_shell()
        self.load_all_settings()

        # Bind window-level responsive resize listener
        self.bind("<Configure>", self._on_root_configure)

        # Start event loop tickers
        self.after(120, lambda: self._apply_responsive_reflow(force=True))
        self.after(200, self._tick_loop)
        self.protocol("WM_DELETE_WINDOW", self._on_close_window)

        if not GUI_CONFIG.exists():
            self.after(500, self._show_first_run_welcome)

    # -----------------------------------------------------------------------
    # Responsive Breakpoint & Reflow Engine
    # -----------------------------------------------------------------------
    def _on_root_configure(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if event.widget is not self:
            return
        if self._resize_debounce_id:
            try:
                self.after_cancel(self._resize_debounce_id)
            except Exception:
                pass
        self._resize_debounce_id = self.after(
            45,
            lambda w=event.width, h=event.height: self._handle_debounced_resize(w, h),
        )

    def _handle_debounced_resize(self, win_w: int, win_h: int) -> None:
        self._resize_debounce_id = None
        new_vp, changed = ViewportBreakpointController.evaluate_viewport(win_w, win_h, self.viewport)
        self.viewport = new_vp
        if changed:
            self._apply_responsive_reflow(force=False)

    def toggle_sidebar_collapse(self) -> None:
        """Allows the user to manually collapse/expand the navigation rail."""
        target = not self.viewport.sidebar_collapsed
        self.viewport.sidebar_user_override = target
        self.viewport.sidebar_collapsed = target
        new_vp, _ = ViewportBreakpointController.evaluate_viewport(
            self.winfo_width(),
            self.winfo_height(),
            self.viewport,
        )
        self.viewport = new_vp
        self._apply_responsive_reflow(force=True)

    def _apply_responsive_reflow(self, force: bool = False) -> None:
        """
        Deterministically updates sidebar rail mode, KPI card grid columns,
        dashboard control bar wrapping, and form column reflow across all pages.
        """
        if self.lifecycle.is_reflowing_layout_flag:
            return
        self.lifecycle.is_reflowing_layout_flag = True
        try:
            vp = self.viewport

            # 1. Reflow Collapsible Sidebar Rail (proportional to window width)
            target_sb_w = vp.sidebar_width
            self.sidebar.configure(width=target_sb_w)
            if vp.sidebar_collapsed:
                self.brand_text_lbl.pack_forget()
                self.ver_badge.pack_forget()
                self.brand_subtitle_lbl.pack_forget()
                self.sidebar_footer.pack_forget()
                self.btn_collapse_rail.configure(text="▶")
            else:
                if not self.brand_text_lbl.winfo_ismapped():
                    self.brand_text_lbl.pack(side="left")
                if not self.ver_badge.winfo_ismapped():
                    self.ver_badge.pack(side="right")
                if not self.brand_subtitle_lbl.winfo_ismapped():
                    self.brand_subtitle_lbl.pack(anchor="w", pady=(4, 0))
                if not self.sidebar_footer.winfo_ismapped():
                    self.sidebar_footer.pack(side="bottom", fill="x", padx=12, pady=12)
                self.btn_collapse_rail.configure(text="◀")

            desc_wrap = max(target_sb_w - 44, 120)
            for pid, widgets in self.nav_buttons.items():
                if vp.sidebar_collapsed:
                    widgets["title"].configure(text=widgets["icon_only"], anchor="center", font=("Segoe UI Emoji", 13))
                    widgets["desc"].pack_forget()
                    widgets["inner"].configure(padx=4, pady=10)
                else:
                    widgets["title"].configure(
                        text=widgets["full_title"],
                        anchor="w",
                        font=("Microsoft JhengHei UI", 10, "bold"),
                    )
                    widgets["desc"].configure(wraplength=desc_wrap)
                    if not widgets["desc"].winfo_ismapped():
                        widgets["desc"].pack(fill="x", pady=(2, 0))
                    widgets["inner"].configure(padx=12, pady=9)

            # 2. Reflow Dashboard 4 KPI Cards (1x4 wide vs 2x2 compact)
            cols = vp.kpi_columns
            for c in range(4):
                self.kpi_row.columnconfigure(c, weight=1 if c < cols else 0, uniform="kpi" if c < cols else "")

            card_wrap_px = max((vp.workspace_width - 64) // max(cols, 1) - 28, 130)
            split_wrap_px = max(card_wrap_px // 2 - 12, 72)
            for idx, key in enumerate(("status", "persona", "social", "engine")):
                card_info = self.kpi_cards[key]
                frame = card_info["frame"]
                r = idx // cols
                c = idx % cols
                pad_l = 0 if c == 0 else 5
                pad_r = 0 if c == (cols - 1) else 5
                pad_b = 6 if (cols == 2 and r == 0) else 0
                frame.grid_configure(row=r, column=c, sticky="nsew", padx=(pad_l, pad_r), pady=(0, pad_b))
                if "subs" in card_info:
                    for sub_lbl in card_info["subs"]:
                        sub_lbl.configure(wraplength=split_wrap_px)
                elif "sub" in card_info and card_info["sub"]:
                    card_info["sub"].configure(wraplength=card_wrap_px)

            # 3. Reflow Dashboard Action & Search Toolbar (1 row vs 2 stacked rows)
            if vp.compact_toolbar:
                self.ctrl_left_group.pack_configure(side="top", fill="x", pady=(0, 6))
                self.ctrl_right_group.pack_configure(side="top", fill="x")
            else:
                self.ctrl_left_group.pack_configure(side="left", fill="none", pady=0)
                self.ctrl_right_group.pack_configure(side="right", fill="none")

            # 4. Reflow Persona Studio Visual Form (2-column vs 1-column)
            if vp.dual_col_forms:
                self.persona_visual_frame.columnconfigure(0, weight=1, uniform="pcol")
                self.persona_visual_frame.columnconfigure(1, weight=1, uniform="pcol")
                self.p_left_col.grid_configure(row=1, column=0, columnspan=1, sticky="nsew", padx=(0, 8), pady=0)
                self.p_right_col.grid_configure(row=1, column=1, columnspan=1, sticky="nsew", padx=(8, 0), pady=0)
            else:
                self.persona_visual_frame.columnconfigure(0, weight=1, uniform="")
                self.persona_visual_frame.columnconfigure(1, weight=0, uniform="")
                self.p_left_col.grid_configure(row=1, column=0, columnspan=2, sticky="nsew", padx=0, pady=(0, 6))
                self.p_right_col.grid_configure(row=2, column=0, columnspan=2, sticky="nsew", padx=0, pady=(0, 4))

            # 5. Reflow System Diagnostics Grid (2-column vs 1-column)
            if hasattr(self, "diag_cells") and self.diag_cells:
                d_cols = 2 if vp.dual_col_forms else 1
                for c in range(2):
                    self.diag_grid.columnconfigure(c, weight=1 if c < d_cols else 0, uniform="diag" if c < d_cols else "")
                for idx, cell in enumerate(self.diag_cells):
                    r, c = divmod(idx, d_cols)
                    cell.grid_configure(row=r, column=c, sticky="ew", padx=4, pady=4)

            # 6. Redraw Pipeline Stepper Canvas for updated width
            self.pipeline_canvas.redraw()
        finally:
            self.lifecycle.is_reflowing_layout_flag = False

    # -----------------------------------------------------------------------
    # Style & Shell Construction
    # -----------------------------------------------------------------------
    def _configure_ttk_styles(self) -> None:
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("TFrame", background=PALETTE["bg_app"])
        st.configure(
            "Vertical.TScrollbar",
            background=PALETTE["bg_card_alt"],
            troughcolor=PALETTE["bg_input"],
            bordercolor=PALETTE["bg_input"],
            arrowcolor=PALETTE["text_secondary"],
        )

    def _create_card(self, parent: tk.Widget, title: str = "", subtitle: str = "", pad: int = 16) -> tk.Frame:
        outer = tk.Frame(
            parent,
            bg=PALETTE["bg_card"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=pad,
            pady=pad,
        )
        if title:
            hdr = tk.Frame(outer, bg=PALETTE["bg_card"])
            hdr.pack(fill="x", pady=(0, 10))
            tk.Label(
                hdr,
                text=title,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_primary"],
                font=("Microsoft JhengHei UI", 11, "bold"),
            ).pack(side="left")
            if subtitle:
                tk.Label(
                    hdr,
                    text=subtitle,
                    bg=PALETTE["bg_card"],
                    fg=PALETTE["text_secondary"],
                    font=("Microsoft JhengHei UI", 9),
                ).pack(side="left", padx=(10, 0))
        return outer

    def _build_shell(self) -> None:
        # Left Collapsible Sidebar Navigation Rail (width scaled proportionally to window width)
        self.sidebar = tk.Frame(
            self,
            bg=PALETTE["bg_sidebar"],
            width=self.viewport.sidebar_width,
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
        )
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        # Brand Header + Collapse Toggle Button
        brand_box = tk.Frame(self.sidebar, bg=PALETTE["bg_sidebar"], padx=14, pady=18)
        brand_box.pack(fill="x")
        title_row = tk.Frame(brand_box, bg=PALETTE["bg_sidebar"])
        title_row.pack(fill="x")

        self.brand_dot = tk.Label(
            title_row,
            text="●",
            bg=PALETTE["bg_sidebar"],
            fg=PALETTE["text_muted"],
            font=("Segoe UI", 13),
        )
        self.brand_dot.pack(side="left", padx=(0, 6))

        self.brand_text_lbl = tk.Label(
            title_row,
            text="DIANA",
            bg=PALETTE["bg_sidebar"],
            fg=PALETTE["text_primary"],
            font=("Segoe UI", 17, "bold"),
        )
        self.brand_text_lbl.pack(side="left")

        self.btn_collapse_rail = tk.Label(
            title_row,
            text="◀",
            bg=PALETTE["bg_card_alt"],
            fg=PALETTE["text_secondary"],
            font=("Segoe UI", 8, "bold"),
            padx=6,
            pady=2,
            cursor="hand2",
        )
        self.btn_collapse_rail.pack(side="right", padx=(4, 0))
        self.btn_collapse_rail.bind("<Button-1>", lambda _e: self.toggle_sidebar_collapse())

        self.ver_badge = tk.Label(
            title_row,
            text="v—",
            bg=PALETTE["accent_soft"],
            fg="#A5B4FC",
            font=("Consolas", 8, "bold"),
            padx=6,
            pady=2,
        )
        self.ver_badge.pack(side="right")

        self.brand_subtitle_lbl = tk.Label(
            brand_box,
            text="Persistent AI Companion 中控台",
            bg=PALETTE["bg_sidebar"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 8),
        )
        self.brand_subtitle_lbl.pack(anchor="w", pady=(4, 0))

        # Navigation Items
        nav_items = [
            ("dashboard", "📊", "📊  即時儀表板", "狀態監控、管線與執行日誌"),
            ("connections", "🔌", "🔌  連線與金鑰", "Local / Web / Image API 設定"),
            ("persona", "🎭", "🎭  角色設定工坊", "視覺化表單、JSON 與 AI 生成"),
            ("memory", "🧠", "🧠  記憶管理中心", "編輯、匯出與匯入記憶庫"),
            ("system", "🛠️", "🛠️  系統診斷與演化", "環境檢查、參數與主動演化"),
        ]
        self.nav_buttons: Dict[str, Dict[str, Any]] = {}
        nav_container = tk.Frame(self.sidebar, bg=PALETTE["bg_sidebar"], padx=8)
        nav_container.pack(fill="x", pady=4)

        for page_id, icon_only, title_text, desc_text in nav_items:
            item_frame = tk.Frame(nav_container, bg=PALETTE["bg_sidebar"], cursor="hand2")
            item_frame.pack(fill="x", pady=3)
            indicator = tk.Frame(item_frame, bg=PALETTE["bg_sidebar"], width=4)
            indicator.pack(side="left", fill="y")
            inner = tk.Frame(item_frame, bg=PALETTE["bg_sidebar"], padx=12, pady=9)
            inner.pack(side="left", fill="both", expand=True)
            lbl_title = tk.Label(
                inner,
                text=title_text,
                bg=PALETTE["bg_sidebar"],
                fg=PALETTE["text_primary"],
                font=("Microsoft JhengHei UI", 10, "bold"),
                anchor="w",
                cursor="hand2",
            )
            lbl_title.pack(fill="x")
            lbl_desc = tk.Label(
                inner,
                text=desc_text,
                bg=PALETTE["bg_sidebar"],
                fg=PALETTE["text_muted"],
                font=("Microsoft JhengHei UI", 8),
                anchor="w",
                cursor="hand2",
            )
            lbl_desc.pack(fill="x", pady=(2, 0))

            self.nav_buttons[page_id] = {
                "frame": item_frame,
                "indicator": indicator,
                "inner": inner,
                "title": lbl_title,
                "desc": lbl_desc,
                "icon_only": icon_only,
                "full_title": title_text,
            }
            for w in (item_frame, indicator, inner, lbl_title, lbl_desc):
                w.bind("<Button-1>", lambda _e, pid=page_id: self.switch_page(pid))

        # Sidebar Footer Quick Summary
        self.sidebar_footer = tk.Frame(
            self.sidebar,
            bg=PALETTE["bg_card"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=12,
            pady=10,
        )
        self.sidebar_footer.pack(side="bottom", fill="x", padx=12, pady=12)
        self.sidebar_status_lbl = tk.Label(
            self.sidebar_footer,
            text="狀態：OFFLINE (未啟動)",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
            anchor="w",
        )
        self.sidebar_status_lbl.pack(fill="x")
        self.sidebar_sub_lbl = tk.Label(
            self.sidebar_footer,
            text="PID: —  |  Backend: —",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_muted"],
            font=("Consolas", 8),
            anchor="w",
        )
        self.sidebar_sub_lbl.pack(fill="x", pady=(4, 0))

        # Main Workspace Area
        self.workspace = tk.Frame(self, bg=PALETTE["bg_app"])
        self.workspace.pack(side="left", fill="both", expand=True)

        # Top Bar (Page Header + Patch Pusher Quick Button + Non-blocking Toast Notification)
        self.topbar = tk.Frame(self.workspace, bg=PALETTE["bg_app"], padx=20, pady=12)
        self.topbar.pack(fill="x")
        self.page_title_lbl = tk.Label(
            self.topbar,
            text="即時儀表板",
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 16, "bold"),
        )
        self.page_title_lbl.pack(side="left")

        if PATCH_PUSHER_SCRIPT.exists() or PATCH_PUSHER_EXE.exists():
            ModernButton(
                self.topbar,
                "🩹 開啟補丁推送工具",
                self.action_open_patch_pusher,
                variant="secondary",
                padx=12,
                pady=5,
            ).pack(side="right", padx=(8, 0))

        if SEED_INSTALLER_EXE.exists() or SEED_INSTALLER_BUILD_BAT.exists():
            ModernButton(
                self.topbar,
                "🌱 冷安裝與 .seed 代理",
                self.action_open_seed_installer,
                variant="secondary",
                padx=12,
                pady=5,
            ).pack(side="right", padx=(8, 0))

        self.toast_lbl = tk.Label(
            self.topbar,
            text="",
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
            padx=12,
            pady=5,
        )
        self.toast_lbl.pack(side="right")

        # Content Pages Container
        self.pages_container = tk.Frame(self.workspace, bg=PALETTE["bg_app"], padx=20, pady=2)
        self.pages_container.pack(fill="both", expand=True, pady=(0, 14))

        self.pages: Dict[str, tk.Frame] = {
            pid: tk.Frame(self.pages_container, bg=PALETTE["bg_app"])
            for pid in ("dashboard", "connections", "persona", "memory", "system")
        }

        self._build_dashboard_page()
        self._build_connections_page()
        self._build_persona_page()
        self._build_memory_page()
        self._build_system_page()
        self.switch_page("dashboard")

    def switch_page(self, page_id: str) -> None:
        self.lifecycle.active_page = page_id
        titles = {
            "dashboard": "即時儀表板 (Runtime Dashboard)",
            "connections": "連線與獨立憑證設定 (API & Credentials)",
            "persona": "角色設定工坊 (Persona Studio)",
            "memory": "記憶管理中心 (Memory Studio — 編輯 / 匯出 / 匯入)",
            "system": "系統診斷與演化控制 (System & Evolution)",
        }
        self.page_title_lbl.configure(text=titles.get(page_id, "Diana Control Center"))

        for pid, widgets in self.nav_buttons.items():
            active = pid == page_id
            bg = PALETTE["bg_card"] if active else PALETTE["bg_sidebar"]
            ind_color = PALETTE["accent"] if active else PALETTE["bg_sidebar"]
            fg_title = "#FFFFFF" if active else PALETTE["text_secondary"]
            widgets["frame"].configure(bg=bg)
            widgets["inner"].configure(bg=bg)
            widgets["indicator"].configure(bg=ind_color)
            widgets["title"].configure(bg=bg, fg=fg_title)
            widgets["desc"].configure(bg=bg)

        for f in self.pages.values():
            f.pack_forget()
        self.pages[page_id].pack(fill="both", expand=True)

        if page_id == "system":
            self.refresh_file_diagnostics()
        self.after(20, lambda: self._apply_responsive_reflow(force=True))

    def show_toast(self, message: str, level: str = "success", duration_ms: int = 4200) -> None:
        """Displays a sleek non-blocking status toast in the top bar."""
        if self._toast_after_id:
            try:
                self.after_cancel(self._toast_after_id)
            except Exception:
                pass
        colors = {
            "success": (PALETTE["success_bg"], "#34D399"),
            "warn": (PALETTE["warning_bg"], "#FBBF24"),
            "error": (PALETTE["danger_bg"], "#F87171"),
            "info": (PALETTE["accent_soft"], "#A5B4FC"),
        }
        bg, fg = colors.get(level, colors["info"])
        self.toast_lbl.configure(text=message, bg=bg, fg=fg)
        self._toast_after_id = self.after(
            duration_ms,
            lambda: self.toast_lbl.configure(text="", bg=PALETTE["bg_app"]),
        )

    # -----------------------------------------------------------------------
    # Page 1: Responsive Dashboard (Reflowing KPI Grid + Draggable Split Pane)
    # -----------------------------------------------------------------------
    def _build_dashboard_page(self) -> None:
        p = self.pages["dashboard"]

        # Resizable Vertical Split Pane between Telemetry Upper Half & Console Lower Half
        self.dashboard_paned = tk.PanedWindow(
            p,
            orient="vertical",
            bg=PALETTE["bg_app"],
            bd=0,
            sashwidth=6,
            sashrelief="flat",
            sashpad=2,
            opaqueresize=True,
        )
        self.dashboard_paned.pack(fill="both", expand=True)

        upper_pane = tk.Frame(self.dashboard_paned, bg=PALETTE["bg_app"])
        lower_pane = tk.Frame(self.dashboard_paned, bg=PALETTE["bg_app"])

        # Top Section 1: Reflowing 4 Telemetry KPI Cards (1x4 on Wide/Standard, 2x2 on Compact)
        self.kpi_row = tk.Frame(upper_pane, bg=PALETTE["bg_app"])
        self.kpi_row.pack(fill="x", pady=(0, 10))
        for col in range(4):
            self.kpi_row.columnconfigure(col, weight=1, uniform="kpi")

        self.kpi_cards: Dict[str, Dict[str, Any]] = {}

        def _bind_interactive_block(
            block_frame: tk.Frame,
            children: List[tk.Widget],
            on_click: Callable[[], None],
            normal_bg: str = PALETTE["bg_card"],
            hover_bg: str = PALETTE["bg_card_alt"],
        ) -> None:
            block_frame.configure(cursor="hand2")
            for w in children:
                w.configure(cursor="hand2")

            def _enter(_e: Any) -> None:
                block_frame.configure(bg=hover_bg, highlightbackground=PALETTE["border_focus"])
                for w in children:
                    try:
                        w.configure(bg=hover_bg)
                    except Exception:
                        pass

            def _leave(_e: Any) -> None:
                block_frame.configure(bg=normal_bg, highlightbackground=PALETTE["border"])
                for w in children:
                    try:
                        w.configure(bg=normal_bg)
                    except Exception:
                        pass

            for w in [block_frame] + children:
                w.bind("<Enter>", _enter)
                w.bind("<Leave>", _leave)
                w.bind("<Button-1>", lambda _e: on_click())

        # KPI Card 0: Runtime Status
        c_status = tk.Frame(
            self.kpi_row,
            bg=PALETTE["bg_card"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=14,
            pady=10,
        )
        c_status.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        tk.Label(
            c_status,
            text="執行狀態 (RUNTIME)",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_muted"],
            font=("Segoe UI", 8, "bold"),
            anchor="w",
        ).pack(fill="x")
        lbl_status_val = tk.Label(
            c_status,
            text="OFFLINE",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 13, "bold"),
            anchor="w",
        )
        lbl_status_val.pack(fill="x", pady=(3, 1))
        lbl_status_sub = tk.Label(
            c_status,
            text="尚未啟動 bot.py",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9),
            anchor="w",
            justify="left",
        )
        lbl_status_sub.pack(fill="x")
        self.kpi_cards["status"] = {"frame": c_status, "val": lbl_status_val, "sub": lbl_status_sub}

        # KPI Card 1: Active Persona (Clickable Quick-Switch for config/*.persona)
        c_persona = tk.Frame(
            self.kpi_row,
            bg=PALETTE["bg_card"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=14,
            pady=10,
        )
        c_persona.grid(row=0, column=1, sticky="nsew", padx=5)
        lbl_persona_hdr = tk.Label(
            c_persona,
            text="目前角色 (PERSONA) · 點擊切換 ▾",
            bg=PALETTE["bg_card"],
            fg="#A5B4FC",
            font=("Microsoft JhengHei UI", 8, "bold"),
            anchor="w",
        )
        lbl_persona_hdr.pack(fill="x")
        lbl_persona_val = tk.Label(
            c_persona,
            text="黛安娜",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 13, "bold"),
            anchor="w",
        )
        lbl_persona_val.pack(fill="x", pady=(3, 1))
        lbl_persona_sub = tk.Label(
            c_persona,
            text="點擊快速切換 config/*.persona",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9),
            anchor="w",
            justify="left",
        )
        lbl_persona_sub.pack(fill="x")
        _bind_interactive_block(
            c_persona,
            [lbl_persona_hdr, lbl_persona_val, lbl_persona_sub],
            self._open_persona_quick_switch_dialog,
        )
        self.kpi_cards["persona"] = {"frame": c_persona, "val": lbl_persona_val, "sub": lbl_persona_sub}

        # KPI Card 2: Split into [關係 (訪客)] and [排程 (提醒)] Interactive Sub-Blocks
        c_social_wrap = tk.Frame(self.kpi_row, bg=PALETTE["bg_app"])
        c_social_wrap.grid(row=0, column=2, sticky="nsew", padx=5)
        c_social_wrap.columnconfigure(0, weight=1, uniform="social_split")
        c_social_wrap.columnconfigure(1, weight=1, uniform="social_split")
        c_social_wrap.rowconfigure(0, weight=1)

        c_rel = tk.Frame(
            c_social_wrap,
            bg=PALETTE["bg_card"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=10,
            pady=8,
        )
        c_rel.grid(row=0, column=0, sticky="nsew", padx=(0, 3))
        lbl_rel_hdr = tk.Label(
            c_rel,
            text="👥 關係 (訪客) ▾",
            bg=PALETTE["bg_card"],
            fg="#A5B4FC",
            font=("Microsoft JhengHei UI", 8, "bold"),
            anchor="w",
        )
        lbl_rel_hdr.pack(fill="x")
        self.kpi_rel_val = tk.Label(
            c_rel,
            text="0 位訪客",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 11, "bold"),
            anchor="w",
        )
        self.kpi_rel_val.pack(fill="x", pady=(3, 1))
        self.kpi_rel_sub = tk.Label(
            c_rel,
            text="點擊查看關係與筆記",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 8),
            anchor="w",
            justify="left",
        )
        self.kpi_rel_sub.pack(fill="x")
        _bind_interactive_block(
            c_rel,
            [lbl_rel_hdr, self.kpi_rel_val, self.kpi_rel_sub],
            self._open_relationships_quick_view_dialog,
        )

        c_sched = tk.Frame(
            c_social_wrap,
            bg=PALETTE["bg_card"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=10,
            pady=8,
        )
        c_sched.grid(row=0, column=1, sticky="nsew", padx=(3, 0))
        lbl_sched_hdr = tk.Label(
            c_sched,
            text="⏰ 排程 (提醒) ▾",
            bg=PALETTE["bg_card"],
            fg=PALETTE["cyan"],
            font=("Microsoft JhengHei UI", 8, "bold"),
            anchor="w",
        )
        lbl_sched_hdr.pack(fill="x")
        self.kpi_sched_val = tk.Label(
            c_sched,
            text="0 筆排程",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 11, "bold"),
            anchor="w",
        )
        self.kpi_sched_val.pack(fill="x", pady=(3, 1))
        self.kpi_sched_sub = tk.Label(
            c_sched,
            text="點擊查看目前排程",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 8),
            anchor="w",
            justify="left",
        )
        self.kpi_sched_sub.pack(fill="x")
        _bind_interactive_block(
            c_sched,
            [lbl_sched_hdr, self.kpi_sched_val, self.kpi_sched_sub],
            self._open_schedules_quick_view_dialog,
        )

        self.kpi_cards["social"] = {
            "frame": c_social_wrap,
            "val": self.kpi_rel_val,
            "sub": self.kpi_rel_sub,
            "subs": [self.kpi_rel_sub, self.kpi_sched_sub],
        }

        # KPI Card 3: Split into [👑 主人推理引擎] and [👤 訪客推理引擎] Interactive Sub-Blocks
        c_engine_wrap = tk.Frame(self.kpi_row, bg=PALETTE["bg_app"])
        c_engine_wrap.grid(row=0, column=3, sticky="nsew", padx=(5, 0))
        c_engine_wrap.columnconfigure(0, weight=1, uniform="engine_split")
        c_engine_wrap.columnconfigure(1, weight=1, uniform="engine_split")
        c_engine_wrap.rowconfigure(0, weight=1)

        c_eng_master = tk.Frame(
            c_engine_wrap,
            bg=PALETTE["bg_card"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=10,
            pady=8,
        )
        c_eng_master.grid(row=0, column=0, sticky="nsew", padx=(0, 3))
        lbl_em_hdr = tk.Label(
            c_eng_master,
            text="👑 主人引擎 ▾",
            bg=PALETTE["bg_card"],
            fg="#FBBF24",
            font=("Microsoft JhengHei UI", 8, "bold"),
            anchor="w",
        )
        lbl_em_hdr.pack(fill="x")
        self.kpi_eng_master_val = tk.Label(
            c_eng_master,
            text="Local",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 11, "bold"),
            anchor="w",
        )
        self.kpi_eng_master_val.pack(fill="x", pady=(3, 1))
        self.kpi_eng_master_sub = tk.Label(
            c_eng_master,
            text="點擊快速切換模型",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 8),
            anchor="w",
            justify="left",
        )
        self.kpi_eng_master_sub.pack(fill="x")
        _bind_interactive_block(
            c_eng_master,
            [lbl_em_hdr, self.kpi_eng_master_val, self.kpi_eng_master_sub],
            lambda: self._open_engine_quick_switch_dialog("master"),
        )

        c_eng_guest = tk.Frame(
            c_engine_wrap,
            bg=PALETTE["bg_card"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=10,
            pady=8,
        )
        c_eng_guest.grid(row=0, column=1, sticky="nsew", padx=(3, 0))
        lbl_eg_hdr = tk.Label(
            c_eng_guest,
            text="👤 訪客引擎 ▾",
            bg=PALETTE["bg_card"],
            fg=PALETTE["cyan"],
            font=("Microsoft JhengHei UI", 8, "bold"),
            anchor="w",
        )
        lbl_eg_hdr.pack(fill="x")
        self.kpi_eng_guest_val = tk.Label(
            c_eng_guest,
            text="Local",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 11, "bold"),
            anchor="w",
        )
        self.kpi_eng_guest_val.pack(fill="x", pady=(3, 1))
        self.kpi_eng_guest_sub = tk.Label(
            c_eng_guest,
            text="點擊快速切換模型",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 8),
            anchor="w",
            justify="left",
        )
        self.kpi_eng_guest_sub.pack(fill="x")
        _bind_interactive_block(
            c_eng_guest,
            [lbl_eg_hdr, self.kpi_eng_guest_val, self.kpi_eng_guest_sub],
            lambda: self._open_engine_quick_switch_dialog("guest"),
        )

        self.kpi_cards["engine"] = {
            "frame": c_engine_wrap,
            "val": self.kpi_eng_master_val,
            "sub": self.kpi_eng_master_sub,
            "subs": [self.kpi_eng_master_sub, self.kpi_eng_guest_sub],
        }

        # Top Section 2: Responsive Vector Pipeline Stepper Card
        pipe_card = self._create_card(
            upper_pane,
            title="即時處理管線 (Processing Pipeline)",
            subtitle="自動追蹤 data/runtime_status.json 工作階段（可拉動下方分隔線調整終端機高度）",
            pad=12,
        )
        pipe_card.pack(fill="x", pady=(0, 6))

        self.pipeline_canvas = PipelineStepperCanvas(pipe_card)
        self.pipeline_canvas.pack(fill="x")

        pipe_footer = tk.Frame(pipe_card, bg=PALETTE["bg_card"])
        pipe_footer.pack(fill="x", pady=(6, 0))
        self.stage_detail_lbl = tk.Label(
            pipe_footer,
            text="目前狀態：尚未啟動 Diana Bot",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
        )
        self.stage_detail_lbl.pack(side="left")
        self.stage_meta_lbl = tk.Label(
            pipe_footer,
            text="最後更新：—",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_muted"],
            font=("Consolas", 9),
        )
        self.stage_meta_lbl.pack(side="right")

        # Lower Section 1: Responsive Bot Control Action Bar & Console Filter
        self.ctrl_bar = tk.Frame(lower_pane, bg=PALETTE["bg_app"])
        self.ctrl_bar.pack(fill="x", pady=(4, 8))

        self.ctrl_left_group = tk.Frame(self.ctrl_bar, bg=PALETTE["bg_app"])
        self.ctrl_left_group.pack(side="left")

        self.btn_start = ModernButton(self.ctrl_left_group, "▶  啟動 Diana", self.action_start_bot, variant="success", padx=15, pady=7)
        self.btn_start.pack(side="left", padx=(0, 6))

        self.btn_stop = ModernButton(self.ctrl_left_group, "■  停止", self.action_stop_bot, variant="danger", padx=13, pady=7)
        self.btn_stop.pack(side="left", padx=(0, 6))

        self.btn_restart = ModernButton(self.ctrl_left_group, "↻  重新啟動", self.action_restart_bot, variant="secondary", padx=13, pady=7)
        self.btn_restart.pack(side="left", padx=(0, 6))

        # Right side log controls group (wraps cleanly below left group on compact widths)
        self.ctrl_right_group = tk.Frame(self.ctrl_bar, bg=PALETTE["bg_app"])
        self.ctrl_right_group.pack(side="right")

        ModernButton(self.ctrl_right_group, "清除日誌", self.clear_log_console, variant="ghost", padx=10, pady=6).pack(side="right")
        ModernButton(self.ctrl_right_group, "複製日誌", self.copy_log_console, variant="ghost", padx=10, pady=6).pack(side="right", padx=(0, 6))

        chk_auto = tk.Checkbutton(
            self.ctrl_right_group,
            text="自動捲動",
            variable=self.auto_scroll_var,
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_secondary"],
            selectcolor=PALETTE["bg_input"],
            activebackground=PALETTE["bg_app"],
            activeforeground=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 9),
        )
        chk_auto.pack(side="right", padx=6)

        search_box = tk.Frame(
            self.ctrl_right_group,
            bg=PALETTE["bg_input"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=8,
            pady=4,
        )
        search_box.pack(side="right", padx=4)
        tk.Label(search_box, text="🔍 篩選:", bg=PALETTE["bg_input"], fg=PALETTE["text_muted"], font=("Microsoft JhengHei UI", 9)).pack(side="left")
        filter_entry = tk.Entry(
            search_box,
            textvariable=self.log_filter_var,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            insertbackground="#FFFFFF",
            relief="flat",
            width=16,
            font=("Consolas", 9),
        )
        filter_entry.pack(side="left", padx=4)
        self.log_filter_var.trace_add("write", lambda *_: self._rerender_filtered_logs())

        # Lower Section 2: Color-Coded Terminal Console (expands to fill remaining height)
        log_card = self._create_card(lower_pane, title="", pad=8)
        log_card.pack(fill="both", expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_card,
            height=10,
            bg=PALETTE["bg_input"],
            fg="#E2E8F0",
            insertbackground="#FFFFFF",
            font=("Consolas", 10),
            relief="flat",
            padx=10,
            pady=8,
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_config("system", foreground="#818CF8", font=("Consolas", 10, "bold"))
        self.log_text.tag_config("success", foreground="#34D399")
        self.log_text.tag_config("warn", foreground="#FBBF24")
        self.log_text.tag_config("error", foreground="#F87171")
        self.log_text.tag_config("metric", foreground="#22D3EE")

        self.dashboard_paned.add(upper_pane, minsize=210, stretch="never")
        self.dashboard_paned.add(lower_pane, minsize=180, stretch="always")

    # -----------------------------------------------------------------------
    # Page 2: Responsive Connections & Independent Credentials
    # -----------------------------------------------------------------------
    def _create_labeled_entry(
        self,
        parent: tk.Widget,
        label_text: str,
        var: tk.StringVar,
        hint_text: str = "",
        secret: bool = False,
    ) -> tk.Entry:
        row = tk.Frame(parent, bg=PALETTE["bg_card"])
        row.pack(fill="x", pady=6)
        row.columnconfigure(1, weight=1)

        lbl_col = tk.Frame(row, bg=PALETTE["bg_card"], width=185)
        lbl_col.grid(row=0, column=0, sticky="nw", padx=(0, 12))
        tk.Label(
            lbl_col,
            text=label_text,
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x")
        if hint_text:
            tk.Label(
                lbl_col,
                text=hint_text,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_muted"],
                font=("Microsoft JhengHei UI", 8),
                anchor="w",
            ).pack(fill="x")

        entry_wrap = tk.Frame(
            row,
            bg=PALETTE["bg_input"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=10,
            pady=6,
        )
        entry_wrap.grid(row=0, column=1, sticky="ew")

        entry = tk.Entry(
            entry_wrap,
            textvariable=var,
            show="•" if secret else "",
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            insertbackground="#FFFFFF",
            relief="flat",
            font=("Consolas", 10),
        )
        entry.pack(side="left", fill="x", expand=True)

        if secret:
            def _toggle() -> None:
                cur = entry.cget("show")
                entry.configure(show="" if cur == "•" else "•")
                toggle_btn.configure(text="🙈 隱藏" if cur == "•" else "👁 顯示")

            toggle_btn = tk.Label(
                entry_wrap,
                text="👁 顯示",
                bg=PALETTE["bg_input"],
                fg=PALETTE["text_secondary"],
                font=("Microsoft JhengHei UI", 8, "bold"),
                cursor="hand2",
                padx=6,
            )
            toggle_btn.pack(side="right")
            toggle_btn.bind("<Button-1>", lambda _e: _toggle())

        return entry

    def _build_connections_page(self) -> None:
        p = self.pages["connections"]

        # Segmented Sub-Tab Bar
        tab_bar = tk.Frame(p, bg=PALETTE["bg_card"], highlightthickness=1, highlightbackground=PALETTE["border"], padx=6, pady=6)
        tab_bar.pack(fill="x", pady=(0, 10))

        self.conn_subpages: Dict[str, tk.Frame] = {}
        self.conn_tab_btns: Dict[str, tk.Label] = {}
        conn_tabs = [
            ("local", "🖥️  本地端 Local API"),
            ("web", "🌐  雲端 Web API"),
            ("image", "🖼️  繪圖 Image API"),
            ("keys", "🔑  獨立憑證與金鑰庫"),
        ]

        scroll_host = ResponsiveScrollFrame(p, bg=PALETTE["bg_app"])
        scroll_host.pack(fill="both", expand=True)
        body_wrap = scroll_host.content

        def _switch_conn_tab(tab_key: str) -> None:
            for k, btn in self.conn_tab_btns.items():
                active = k == tab_key
                btn.configure(
                    bg=PALETTE["accent"] if active else PALETTE["bg_card"],
                    fg="#FFFFFF" if active else PALETTE["text_secondary"],
                )
            for f in self.conn_subpages.values():
                f.pack_forget()
            self.conn_subpages[tab_key].pack(fill="both", expand=True)
            scroll_host.after(20, scroll_host._update_scrollregion_and_bar)

        for tab_key, tab_title in conn_tabs:
            btn = tk.Label(
                tab_bar,
                text=tab_title,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_secondary"],
                font=("Microsoft JhengHei UI", 10, "bold"),
                padx=16,
                pady=7,
                cursor="hand2",
            )
            btn.pack(side="left", padx=3)
            btn.bind("<Button-1>", lambda _e, tk_id=tab_key: _switch_conn_tab(tk_id))
            self.conn_tab_btns[tab_key] = btn
            self.conn_subpages[tab_key] = tk.Frame(body_wrap, bg=PALETTE["bg_app"])

        # Sub-tab 1: Local API
        c_local = self._create_card(
            self.conn_subpages["local"],
            title="Local LLM 推理服務",
            subtitle="適用 Ollama (Port 11434) / LM Studio (Port 1234) / vLLM 等 OpenAI 相容端點",
        )
        c_local.pack(fill="x")
        self._create_labeled_entry(c_local, "Base URL", self.vars["local_base_url"], "Ollama: http://127.0.0.1:11434/v1 | LM Studio: http://127.0.0.1:1234/v1")
        self._create_labeled_entry(c_local, "Model Name", self.vars["local_model"], "例: gemma4:e2b 或 google/gemma-4-e2b")
        preset_local = tk.Frame(c_local, bg=PALETTE["bg_card"])
        preset_local.pack(fill="x", pady=(6, 0))
        tk.Label(preset_local, text="快速套用預設：", bg=PALETTE["bg_card"], fg=PALETTE["text_secondary"], font=("Microsoft JhengHei UI", 9)).pack(side="left", padx=(0, 8))
        ModernButton(preset_local, "🦙 Ollama 預設 (11434 + gemma4:e2b)", self.apply_ollama_preset, variant="secondary", padx=10, pady=4).pack(side="left", padx=(0, 8))
        ModernButton(preset_local, "🖥️ LM Studio 預設 (1234 + google/gemma-4-e2b)", self.apply_lmstudio_preset, variant="secondary", padx=10, pady=4).pack(side="left")
        act_local = tk.Frame(c_local, bg=PALETTE["bg_card"])
        act_local.pack(fill="x", pady=(12, 0))
        self.lbl_test_local = tk.Label(act_local, text="● 尚未測試連線", bg=PALETTE["bg_card"], fg=PALETTE["text_muted"], font=("Microsoft JhengHei UI", 9, "bold"))
        self.lbl_test_local.pack(side="left")
        self.btn_test_local = ModernButton(act_local, "⚡ 測試本地 API 連線", self.action_test_local, variant="secondary")
        self.btn_test_local.pack(side="right")

        # Sub-tab 2: Web API
        c_web = self._create_card(
            self.conn_subpages["web"],
            title="Web LLM 雲端推理服務",
            subtitle="獨立於本地 API 的線上模型端點與專屬聊天金鑰",
        )
        c_web.pack(fill="x")
        self._create_labeled_entry(c_web, "Web Base URL", self.vars["web_base_url"], "OpenAI 相容 Endpoint")
        self._create_labeled_entry(c_web, "Web Model", self.vars["web_model"], "例: gemini-3.5-flash-lite")
        self._create_labeled_entry(c_web, "Chat API Key", self.vars["chat_key"], "config/google_api_key_chat.txt", secret=True)
        act_web = tk.Frame(c_web, bg=PALETTE["bg_card"])
        act_web.pack(fill="x", pady=(12, 0))
        self.lbl_test_web = tk.Label(act_web, text="● 尚未測試連線", bg=PALETTE["bg_card"], fg=PALETTE["text_muted"], font=("Microsoft JhengHei UI", 9, "bold"))
        self.lbl_test_web.pack(side="left")
        self.btn_test_web = ModernButton(act_web, "⚡ 測試 Web API 連線", self.action_test_web, variant="secondary")
        self.btn_test_web.pack(side="right")

        # Sub-tab 3: Image API
        c_img = self._create_card(
            self.conn_subpages["image"],
            title="Image Generation 動作插圖生成服務",
            subtitle="由 image_gen.py 讀取，搭配 img/reference 參考圖生成一致角色插圖",
        )
        c_img.pack(fill="x")
        self._create_labeled_entry(c_img, "Image Endpoint", self.vars["image_endpoint"], "Gemini generateContent URL")
        self._create_labeled_entry(c_img, "Image Model", self.vars["image_model"], "例: gemini-3.1-flash-image")
        self._create_labeled_entry(c_img, "Image API Key", self.vars["image_key"], "config/google_api_key_image.txt", secret=True)
        act_img = tk.Frame(c_img, bg=PALETTE["bg_card"])
        act_img.pack(fill="x", pady=(12, 0))
        self.lbl_test_img = tk.Label(act_img, text="● 憑證獨立儲存於 google_api_key_image.txt", bg=PALETTE["bg_card"], fg=PALETTE["text_secondary"], font=("Microsoft JhengHei UI", 9))
        self.lbl_test_img.pack(side="left")
        ModernButton(
            act_img,
            "✓ 驗證設定完整性",
            self.action_verify_image_cfg,
            variant="secondary",
        ).pack(side="right")

        # Sub-tab 4: All Isolated Keys & Evolution Model
        c_keys = self._create_card(
            self.conn_subpages["keys"],
            title="四組完全分離的憑證檔案管理",
            subtitle="嚴格維持 chat / evolution / image / discord 檔案分離，互不覆蓋",
        )
        c_keys.pack(fill="x")
        self._create_labeled_entry(c_keys, "Discord Bot Token", self.vars["discord"], "config/discord_token.txt", secret=True)
        self._create_labeled_entry(c_keys, "Chat API Key", self.vars["chat_key"], "config/google_api_key_chat.txt (第 1 行)", secret=True)
        self._create_labeled_entry(c_keys, "Evolution API Key", self.vars["evolution_key"], "config/google_api_key_evolution.txt (第 1 行)", secret=True)
        self._create_labeled_entry(c_keys, "Evolution 模型名稱", self.vars["evolution_model"], "config/google_api_key_evolution.txt (第 2 行，保留不丟失)")
        self._create_labeled_entry(c_keys, "Image API Key", self.vars["image_key"], "config/google_api_key_image.txt (第 1 行)", secret=True)

        # Sticky Bottom Save Bar
        save_bar = tk.Frame(p, bg=PALETTE["bg_app"])
        save_bar.pack(fill="x", pady=(10, 0))
        tk.Label(
            save_bar,
            text="💡 修改設定並儲存後，若 Diana 正在運行，點選「重新啟動」即可套用最新連線參數。",
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9),
        ).pack(side="left")
        ModernButton(save_bar, "💾  儲存所有連線與金鑰設定", self.save_all_settings, variant="primary", padx=20, pady=8).pack(side="right")

        _switch_conn_tab("local")

    # -----------------------------------------------------------------------
    # Page 3: Dual-Mode Persona Studio (Responsive Visual Form <-> Raw JSON)
    # -----------------------------------------------------------------------
    def _build_persona_page(self) -> None:
        p = self.pages["persona"]

        # Top Toolbar: Presets, AI Generation Prompt, Mode Switch, Save
        top_card = self._create_card(p, pad=12)
        top_card.pack(fill="x", pady=(0, 8))

        btn_row = tk.Frame(top_card, bg=PALETTE["bg_card"])
        btn_row.pack(fill="x", pady=(0, 8))

        ModernButton(btn_row, "📂 讀取目前使用中設定", self.load_current_persona_file, variant="secondary", padx=11, pady=6).pack(side="left", padx=(0, 5))
        ModernButton(btn_row, "👧 載入黛安娜預設範例", self.load_diana_example, variant="secondary", padx=11, pady=6).pack(side="left", padx=5)
        ModernButton(btn_row, "📄 空白角色模板", self.load_blank_persona, variant="secondary", padx=11, pady=6).pack(side="left", padx=5)
        ModernButton(btn_row, "📤 匯出角色檔 (.persona)", self.action_export_persona_file, variant="secondary", padx=11, pady=6).pack(side="left", padx=5)

        ModernButton(btn_row, "💾 儲存 Persona 設定", self.save_persona, variant="primary", padx=15, pady=6).pack(side="right")

        # Mode Switcher Slider (Visual Form <-> Raw JSON)
        self.persona_mode = "visual"
        self.persona_mode_slider = ModeSliderSwitch(
            btn_row,
            on_switch=self.switch_persona_editor_mode,
            initial_mode="visual",
        )
        self.persona_mode_slider.pack(side="right", padx=8)

        # AI Generation Bar
        ai_row = tk.Frame(top_card, bg=PALETTE["bg_card"])
        ai_row.pack(fill="x")
        tk.Label(
            ai_row,
            text="✨ AI 角色生成：",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
        ).pack(side="left")

        prompt_wrap = tk.Frame(ai_row, bg=PALETTE["bg_input"], highlightthickness=1, highlightbackground=PALETTE["border"], padx=8, pady=4)
        prompt_wrap.pack(side="left", fill="x", expand=True, padx=8)
        tk.Entry(
            prompt_wrap,
            textvariable=self.persona_prompt_var,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            insertbackground="#FFFFFF",
            relief="flat",
            font=("Microsoft JhengHei UI", 9),
        ).pack(fill="x")

        rb_local = tk.Radiobutton(
            ai_row,
            text="Local API",
            variable=self.persona_gen_backend_var,
            value="local",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            selectcolor=PALETTE["bg_input"],
            activebackground=PALETTE["bg_card"],
            font=("Segoe UI", 8),
        )
        rb_local.pack(side="left", padx=2)
        rb_web = tk.Radiobutton(
            ai_row,
            text="Web API",
            variable=self.persona_gen_backend_var,
            value="web",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            selectcolor=PALETTE["bg_input"],
            activebackground=PALETTE["bg_card"],
            font=("Segoe UI", 8),
        )
        rb_web.pack(side="left", padx=(0, 6))

        self.btn_gen_persona = ModernButton(ai_row, "✨ 生成三個候選方案", self.action_generate_personas, variant="primary", padx=12, pady=5)
        self.btn_gen_persona.pack(side="right")

        # Editor Stack Container
        self.persona_editor_stack = tk.Frame(p, bg=PALETTE["bg_app"])
        self.persona_editor_stack.pack(fill="both", expand=True)

        # 1) Scrollable Responsive Visual Form Editor Frame
        self.persona_scroll_host = ResponsiveScrollFrame(self.persona_editor_stack, bg=PALETTE["bg_card"])
        self.persona_scroll_host.configure(highlightthickness=1, highlightbackground=PALETTE["border"])
        self.persona_visual_frame = tk.Frame(
            self.persona_scroll_host.content,
            bg=PALETTE["bg_card"],
            padx=16,
            pady=12,
        )
        self.persona_visual_frame.pack(fill="both", expand=True)
        self._build_persona_visual_form(self.persona_visual_frame)

        # 2) Raw JSON Editor Frame
        self.persona_json_frame = tk.Frame(
            self.persona_editor_stack,
            bg=PALETTE["bg_card"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=12,
            pady=12,
        )
        self.persona_text = scrolledtext.ScrolledText(
            self.persona_json_frame,
            bg=PALETTE["bg_input"],
            fg="#E2E8F0",
            insertbackground="#FFFFFF",
            font=("Consolas", 10),
            relief="flat",
            padx=12,
            pady=10,
        )
        self.persona_text.pack(fill="both", expand=True)

        self.persona_scroll_host.pack(fill="both", expand=True)

    def _build_persona_visual_form(self, parent: tk.Frame) -> None:
        """Builds an adaptive structured editor that reflows between 2 columns and 1 column."""
        parent.columnconfigure(0, weight=1, uniform="pcol")
        parent.columnconfigure(1, weight=1, uniform="pcol")

        # Top row: Name & Age/Style
        top_grid = tk.Frame(parent, bg=PALETTE["bg_card"])
        top_grid.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        top_grid.columnconfigure(1, weight=1)
        top_grid.columnconfigure(3, weight=1)

        self.p_name_var = tk.StringVar()
        self.p_age_var = tk.StringVar()

        tk.Label(top_grid, text="角色名稱 (name):", bg=PALETTE["bg_card"], fg=PALETTE["text_primary"], font=("Microsoft JhengHei UI", 9, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 8))
        tk.Entry(top_grid, textvariable=self.p_name_var, bg=PALETTE["bg_input"], fg=PALETTE["text_primary"], insertbackground="#FFFFFF", relief="flat", font=("Microsoft JhengHei UI", 10)).grid(row=0, column=1, sticky="ew", padx=(0, 18), ipady=4)

        tk.Label(top_grid, text="年齡與外型風格 (age_style):", bg=PALETTE["bg_card"], fg=PALETTE["text_primary"], font=("Microsoft JhengHei UI", 9, "bold")).grid(row=0, column=2, sticky="w", padx=(0, 8))
        tk.Entry(top_grid, textvariable=self.p_age_var, bg=PALETTE["bg_input"], fg=PALETTE["text_primary"], insertbackground="#FFFFFF", relief="flat", font=("Microsoft JhengHei UI", 10)).grid(row=0, column=3, sticky="ew", ipady=4)

        # Left column: Background, Relationship, Guest Attitude, Favorability Reactions
        self.p_left_col = tk.Frame(parent, bg=PALETTE["bg_card"])
        self.p_left_col.grid(row=1, column=0, sticky="nsew", padx=(0, 8))

        # Right column: Personality Traits, Speech Patterns, Tone Rules (1 per line)
        self.p_right_col = tk.Frame(parent, bg=PALETTE["bg_card"])
        self.p_right_col.grid(row=1, column=1, sticky="nsew", padx=(8, 0))

        def _make_text_box(col: tk.Frame, title: str, height: int) -> tk.Text:
            tk.Label(col, text=title, bg=PALETTE["bg_card"], fg=PALETTE["text_secondary"], font=("Microsoft JhengHei UI", 9, "bold"), anchor="w").pack(fill="x", pady=(4, 2))
            t = tk.Text(
                col,
                height=height,
                bg=PALETTE["bg_input"],
                fg=PALETTE["text_primary"],
                insertbackground="#FFFFFF",
                relief="flat",
                font=("Microsoft JhengHei UI", 9),
                padx=8,
                pady=6,
                wrap="word",
            )
            t.pack(fill="both", expand=True, pady=(0, 4))
            return t

        self.p_bg_text = _make_text_box(self.p_left_col, "🔒 角色背景設定 (background — 基本核心)", 4)
        self.p_rel_text = _make_text_box(self.p_left_col, "🔒 與主人的關係 (relationship — 基本核心)", 3)
        self.p_guest_text = _make_text_box(self.p_left_col, "🔒 對待訪客態度 (guest_attitude — 基本核心)", 3)
        self.p_favor_text = _make_text_box(self.p_left_col, "🌱 好感度三階段反應 (低 / 中 / 高 — 可演化微調)", 4)

        self.p_traits_text = _make_text_box(self.p_right_col, "🔒 基本核心性格 (core_traits — 演化引擎鎖定保護，每行一條)", 4)
        self.p_learned_traits_text = _make_text_box(self.p_right_col, "🌱 後天演化習得特質 (learned_traits — 由演化引擎成長更新，每行一條)", 4)
        self.p_speech_text = _make_text_box(self.p_right_col, "🔒 說話習慣 (speech_patterns — 基本核心，每行一條)", 4)
        self.p_tone_text = _make_text_box(self.p_right_col, "🔒 語氣與行為規範 (tone_rules — 基本核心，每行一條)", 4)

    def _sync_json_to_visual_form(self, obj: Dict[str, Any]) -> None:
        """Populates the Visual Form fields from a Persona dictionary."""
        self.p_name_var.set(str(obj.get("name", "")))
        self.p_age_var.set(str(obj.get("age_style", "")))

        def _set_txt(widget: tk.Text, content: str) -> None:
            widget.delete("1.0", "end")
            widget.insert("1.0", content)

        _set_txt(self.p_bg_text, str(obj.get("background", "")))
        _set_txt(self.p_rel_text, str(obj.get("relationship", "")))
        _set_txt(self.p_guest_text, str(obj.get("guest_attitude", "")))

        evolvable = obj.get("evolvable_persona", {})
        if not isinstance(evolvable, dict):
            evolvable = {}

        fav = evolvable.get("favorability_reactions") or obj.get("favorability_reactions", {})
        if not isinstance(fav, dict):
            fav = {}
        fav_lines = [
            f"[低] {fav.get('low', '')}",
            f"[中] {fav.get('medium', '')}",
            f"[高] {fav.get('high', '')}",
        ]
        _set_txt(self.p_favor_text, "\n".join(fav_lines))

        all_traits = [str(x).strip() for x in obj.get("personality_traits", []) if str(x).strip()]
        core_traits = obj.get("core_traits")
        if not isinstance(core_traits, list) or not core_traits:
            core_traits = all_traits[:3]
        learned_traits = evolvable.get("learned_traits")
        if not isinstance(learned_traits, list):
            learned_traits = [t for t in all_traits if t not in core_traits]

        _set_txt(self.p_traits_text, "\n".join(str(x) for x in core_traits if str(x).strip()))
        _set_txt(self.p_learned_traits_text, "\n".join(str(x) for x in learned_traits if str(x).strip()))
        _set_txt(self.p_speech_text, "\n".join(str(x) for x in obj.get("speech_patterns", []) if str(x).strip()))
        _set_txt(self.p_tone_text, "\n".join(str(x) for x in obj.get("tone_rules", []) if str(x).strip()))

    def _collect_visual_form_to_dict(self) -> Dict[str, Any]:
        """Merges edits from the Visual Form back into the underlying JSON object."""
        try:
            base_obj = json.loads(self.persona_text.get("1.0", "end"))
            if not isinstance(base_obj, dict):
                base_obj = ConfigRepository.blank_persona_template()
        except Exception:
            base_obj = ConfigRepository.blank_persona_template()

        base_obj["name"] = self.p_name_var.get().strip()
        base_obj["age_style"] = self.p_age_var.get().strip()
        base_obj["background"] = self.p_bg_text.get("1.0", "end").strip()
        base_obj["relationship"] = self.p_rel_text.get("1.0", "end").strip()
        base_obj["guest_attitude"] = self.p_guest_text.get("1.0", "end").strip()

        def _lines(w: tk.Text) -> List[str]:
            items = [line.strip() for line in w.get("1.0", "end").splitlines() if line.strip()]
            return items

        core_traits = _lines(self.p_traits_text)
        learned_traits = _lines(self.p_learned_traits_text)
        combined_traits: List[str] = []
        for t in (core_traits + learned_traits):
            if t and t not in combined_traits:
                combined_traits.append(t)

        base_obj["core_traits"] = core_traits or [""]
        base_obj["speech_patterns"] = _lines(self.p_speech_text) or [""]
        base_obj["tone_rules"] = _lines(self.p_tone_text) or [""]

        fav_raw = [line.strip() for line in self.p_favor_text.get("1.0", "end").splitlines() if line.strip()]
        fav_dict = {"low": "", "medium": "", "high": ""}
        for idx, key in enumerate(("low", "medium", "high")):
            if idx < len(fav_raw):
                val = fav_raw[idx]
                for prefix in ("[低]", "[中]", "[高]"):
                    if val.startswith(prefix):
                        val = val[len(prefix):].strip()
                fav_dict[key] = val

        existing_evolvable = base_obj.get("evolvable_persona", {})
        if not isinstance(existing_evolvable, dict):
            existing_evolvable = {}
        examples = existing_evolvable.get("few_shot_examples") or base_obj.get("few_shot_examples", [])

        base_obj["evolvable_persona"] = {
            "learned_traits": learned_traits,
            "favorability_reactions": fav_dict,
            "few_shot_examples": examples,
        }
        base_obj["personality_traits"] = combined_traits or [""]
        base_obj["favorability_reactions"] = fav_dict
        base_obj["few_shot_examples"] = examples
        return base_obj

    def switch_persona_editor_mode(self, target_mode: str) -> None:
        if target_mode == self.persona_mode:
            if hasattr(self, "persona_mode_slider"):
                self.persona_mode_slider.set_mode(self.persona_mode)
            return
        if target_mode == "json":
            merged = self._collect_visual_form_to_dict()
            self.persona_text.delete("1.0", "end")
            self.persona_text.insert("1.0", json.dumps(merged, ensure_ascii=False, indent=2))
            self.persona_scroll_host.pack_forget()
            self.persona_json_frame.pack(fill="both", expand=True)
        else:
            try:
                parsed = json.loads(self.persona_text.get("1.0", "end"))
                self._sync_json_to_visual_form(parsed)
            except Exception as e:
                messagebox.showerror("JSON 語法錯誤", f"請先修正 JSON 格式再切換至表單模式：\n{e}")
                if hasattr(self, "persona_mode_slider"):
                    self.persona_mode_slider.set_mode(self.persona_mode)
                return
            self.persona_json_frame.pack_forget()
            self.persona_scroll_host.pack(fill="both", expand=True)
        self.persona_mode = target_mode
        if hasattr(self, "persona_mode_slider"):
            self.persona_mode_slider.set_mode(target_mode)

    def _set_persona_object(self, obj: Dict[str, Any]) -> None:
        self.persona_text.delete("1.0", "end")
        self.persona_text.insert("1.0", json.dumps(obj, ensure_ascii=False, indent=2))
        self._sync_json_to_visual_form(obj)

    # -----------------------------------------------------------------------
    # Page 3.5: Memory Studio (Edit, Export & Import All Memories)
    # -----------------------------------------------------------------------
    def _build_memory_page(self) -> None:
        p = self.pages["memory"]

        # Top Action Bar: Full Bundle Export / Import / Reload / Save
        top_card = self._create_card(
            p,
            title="記憶庫總覽與備份中心 (Memory Backup, Export & Import — .memory 專屬格式)",
            subtitle="一鍵將所有對話記憶、主人長效備忘錄、好感度、訪客認知筆記與 ChromaDB 向量記憶庫打包匯出為 .memory 檔或還原匯入",
            pad=12,
        )
        top_card.pack(fill="x", pady=(0, 10))

        top_btn_row = tk.Frame(top_card, bg=PALETTE["bg_card"])
        top_btn_row.pack(fill="x")

        ModernButton(
            top_btn_row,
            "📤 匯出完整記憶包 (.memory)",
            self.action_export_memory_bundle,
            variant="primary",
            padx=14,
            pady=6,
        ).pack(side="left", padx=(0, 8))

        ModernButton(
            top_btn_row,
            "📥 匯入記憶包 (.memory)",
            self.action_import_memory_bundle,
            variant="secondary",
            padx=14,
            pady=6,
        ).pack(side="left", padx=4)

        ModernButton(
            top_btn_row,
            "🔄 重新載入記憶資料",
            lambda: self.load_memory_page_data(silent=False),
            variant="secondary",
            padx=12,
            pady=6,
        ).pack(side="left", padx=4)

        ModernButton(
            top_btn_row,
            "💾 儲存目前記憶修改",
            self.save_memory_page_data,
            variant="success",
            padx=16,
            pady=6,
        ).pack(side="right")

        # Segmented Sub-Tab Bar for Memory Categories
        mem_tab_bar = tk.Frame(
            p,
            bg=PALETTE["bg_card"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=6,
            pady=6,
        )
        mem_tab_bar.pack(fill="x", pady=(0, 10))

        self.mem_subpages: Dict[str, tk.Frame] = {}
        self.mem_tab_btns: Dict[str, tk.Label] = {}
        mem_tabs = [
            ("profile", "📌 主人長效備忘錄 (user_profile)"),
            ("short_term", "💬 短期對話記憶 (memory.json)"),
            ("social_base", "❤️ 好感度、主人綁定與提醒"),
            ("chroma", "🗄️ 長期向量記憶庫 (ChromaDB)"),
        ]

        mem_scroll_host = ResponsiveScrollFrame(p, bg=PALETTE["bg_app"])
        mem_scroll_host.pack(fill="both", expand=True)
        mem_body_wrap = mem_scroll_host.content

        def _switch_mem_tab(tab_key: str) -> None:
            for k, btn in self.mem_tab_btns.items():
                active = k == tab_key
                btn.configure(
                    bg=PALETTE["accent"] if active else PALETTE["bg_card"],
                    fg="#FFFFFF" if active else PALETTE["text_secondary"],
                )
            for f in self.mem_subpages.values():
                f.pack_forget()
            self.mem_subpages[tab_key].pack(fill="both", expand=True)
            mem_scroll_host.after(20, mem_scroll_host._update_scrollregion_and_bar)

        for tab_key, tab_title in mem_tabs:
            btn = tk.Label(
                mem_tab_bar,
                text=tab_title,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_secondary"],
                font=("Microsoft JhengHei UI", 9, "bold"),
                padx=12,
                pady=6,
                cursor="hand2",
            )
            btn.pack(side="left", padx=3)
            btn.bind("<Button-1>", lambda _e, tk_id=tab_key: _switch_mem_tab(tk_id))
            self.mem_tab_btns[tab_key] = btn
            self.mem_subpages[tab_key] = tk.Frame(mem_body_wrap, bg=PALETTE["bg_app"])

        # --- Sub-tab 1: Master Long-Term Profile (data/user_profile.json) ---
        c_prof = self._create_card(
            self.mem_subpages["profile"],
            title="主人長效備忘錄 (data/user_profile.json)",
            subtitle="直接檢視、修改、新增或刪除關於主人的長期習慣與重要設定（點擊左側條目即可在右側編輯）",
        )
        c_prof.pack(fill="both", expand=True)

        self._profile_items_cache: List[str] = []
        prof_split = tk.Frame(c_prof, bg=PALETTE["bg_card"])
        prof_split.pack(fill="both", expand=True, pady=(4, 8))
        prof_split.columnconfigure(0, weight=2)
        prof_split.columnconfigure(1, weight=3)

        prof_left = tk.Frame(prof_split, bg=PALETTE["bg_card"])
        prof_left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        tk.Label(
            prof_left,
            text="備忘錄條目清單：",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 4))

        self.prof_listbox = tk.Listbox(
            prof_left,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            selectbackground=PALETTE["accent"],
            selectforeground="#FFFFFF",
            font=("Microsoft JhengHei UI", 9),
            relief="flat",
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            height=12,
            exportselection=False,
        )
        self.prof_listbox.pack(fill="both", expand=True)
        self.prof_listbox.bind("<<ListboxSelect>>", self._on_profile_listbox_select)

        prof_right = tk.Frame(prof_split, bg=PALETTE["bg_card"])
        prof_right.grid(row=0, column=1, sticky="nsew")
        tk.Label(
            prof_right,
            text="選取條目內容編輯器（修改後請點選「更新選取條目」或「新增為新條目」）：",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 4))

        self.prof_entry_editor = scrolledtext.ScrolledText(
            prof_right,
            height=12,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            insertbackground="#FFFFFF",
            relief="flat",
            font=("Microsoft JhengHei UI", 10),
            padx=10,
            pady=8,
            wrap="word",
        )
        self.prof_entry_editor.pack(fill="both", expand=True)

        prof_actions = tk.Frame(c_prof, bg=PALETTE["bg_card"])
        prof_actions.pack(fill="x", pady=(6, 0))
        ModernButton(prof_actions, "✓ 更新選取條目", self.action_update_profile_entry, variant="primary", padx=12, pady=5).pack(side="left", padx=(0, 6))
        ModernButton(prof_actions, "➕ 新增為新條目", self.action_add_profile_entry, variant="secondary", padx=12, pady=5).pack(side="left", padx=4)
        ModernButton(prof_actions, "🗑️ 刪除選取條目", self.action_delete_profile_entry, variant="danger", padx=12, pady=5).pack(side="left", padx=4)

        # --- Sub-tab 2: Short-Term Conversation Memory (data/memory.json) ---
        c_short = self._create_card(
            self.mem_subpages["short_term"],
            title="各頻道短期對話記憶 (data/memory.json)",
            subtitle="以 JSON 格式檢視或直接編輯各 Discord 頻道的近期對話上下文（若需刪除單句錯誤對話可直接編輯並儲存）",
        )
        c_short.pack(fill="both", expand=True)

        self.mem_short_text = scrolledtext.ScrolledText(
            c_short,
            height=18,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            insertbackground="#FFFFFF",
            relief="flat",
            font=("Consolas", 10),
            padx=12,
            pady=10,
        )
        self.mem_short_text.pack(fill="both", expand=True, pady=(4, 8))

        short_actions = tk.Frame(c_short, bg=PALETTE["bg_card"])
        short_actions.pack(fill="x")
        ModernButton(short_actions, "🗑️ 清空所有頻道短期記憶", self.action_clear_short_term_memory, variant="danger", padx=12, pady=5).pack(side="left")
        ModernButton(short_actions, "💾 儲存短期對話記憶", self.save_memory_page_data, variant="primary", padx=14, pady=5).pack(side="right")

        # --- Sub-tab 3: Favorability, Master Binding & Reminders ---
        c_social = self._create_card(
            self.mem_subpages["social_base"],
            title="主人身分綁定、好感度與提醒排程 (base.json / favorability.json / reminders.json)",
            subtitle="直接檢視或修改專屬主人 Discord ID、通知頻道、管理員密碼，以及各成員好感度數值與提醒事項",
        )
        c_social.pack(fill="both", expand=True)

        self.mem_master_id_var = tk.StringVar()
        self.mem_notify_ch_var = tk.StringVar()
        self.mem_admin_pwd_var = tk.StringVar()

        self._create_labeled_entry(c_social, "主人 Discord ID (master_id)", self.mem_master_id_var, "留空代表未綁定主人（冷啟動保護模式）")
        self._create_labeled_entry(c_social, "系統通知頻道 ID", self.mem_notify_ch_var, "data/base.json (notify_channel_id)")
        self._create_labeled_entry(c_social, "管理員密碼 (admin_password)", self.mem_admin_pwd_var, "預設為 0000")

        tk.Label(
            c_social,
            text="好感度數值對照表 (data/favorability.json — 格式為 {\"Discord用戶ID\": 分數})：",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(10, 4))

        self.mem_favor_text = scrolledtext.ScrolledText(
            c_social,
            height=6,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            insertbackground="#FFFFFF",
            relief="flat",
            font=("Consolas", 10),
            padx=10,
            pady=8,
        )
        self.mem_favor_text.pack(fill="x", pady=(0, 8))

        tk.Label(
            c_social,
            text="提醒與行程清單 (data/reminders.json)：",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(4, 4))

        self.mem_reminders_text = scrolledtext.ScrolledText(
            c_social,
            height=6,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            insertbackground="#FFFFFF",
            relief="flat",
            font=("Consolas", 10),
            padx=10,
            pady=8,
        )
        self.mem_reminders_text.pack(fill="x")

        # --- Sub-tab 4: Long-Term Vector Memory DB (ChromaDB: diana_memory_db) ---
        c_chroma = self._create_card(
            self.mem_subpages["chroma"],
            title="ChromaDB 長期向量記憶庫 (diana_memory_db/chroma.sqlite3)",
            subtitle="直接檢視、修改文字內容或刪除長期 RAG 對話事件總結（匯出完整記憶包時亦會一併打包整個向量庫）",
        )
        c_chroma.pack(fill="both", expand=True)

        self._chroma_rows_cache: List[Tuple[str, str]] = []
        self.lbl_chroma_count = tk.Label(
            c_chroma,
            text="目前向量記憶筆數：0 筆",
            bg=PALETTE["bg_card"],
            fg=PALETTE["accent"],
            font=("Microsoft JhengHei UI", 9, "bold"),
            anchor="w",
        )
        self.lbl_chroma_count.pack(fill="x", pady=(0, 6))

        chroma_split = tk.Frame(c_chroma, bg=PALETTE["bg_card"])
        chroma_split.pack(fill="both", expand=True, pady=(0, 8))
        chroma_split.columnconfigure(0, weight=2)
        chroma_split.columnconfigure(1, weight=3)

        chroma_left = tk.Frame(chroma_split, bg=PALETTE["bg_card"])
        chroma_left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.chroma_listbox = tk.Listbox(
            chroma_left,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            selectbackground=PALETTE["accent"],
            selectforeground="#FFFFFF",
            font=("Microsoft JhengHei UI", 9),
            relief="flat",
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            height=12,
            exportselection=False,
        )
        self.chroma_listbox.pack(fill="both", expand=True)
        self.chroma_listbox.bind("<<ListboxSelect>>", self._on_chroma_listbox_select)

        chroma_right = tk.Frame(chroma_split, bg=PALETTE["bg_card"])
        chroma_right.grid(row=0, column=1, sticky="nsew")
        self.chroma_entry_editor = scrolledtext.ScrolledText(
            chroma_right,
            height=12,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            insertbackground="#FFFFFF",
            relief="flat",
            font=("Microsoft JhengHei UI", 10),
            padx=10,
            pady=8,
            wrap="word",
        )
        self.chroma_entry_editor.pack(fill="both", expand=True)

        chroma_actions = tk.Frame(c_chroma, bg=PALETTE["bg_card"])
        chroma_actions.pack(fill="x")
        ModernButton(chroma_actions, "✓ 更新選取向量記憶文字", self.action_update_selected_chroma_memory, variant="primary", padx=12, pady=5).pack(side="left", padx=(0, 6))
        ModernButton(chroma_actions, "🗑️ 刪除選取向量記憶", self.action_delete_selected_chroma_memory, variant="danger", padx=12, pady=5).pack(side="left", padx=4)
        ModernButton(chroma_actions, "🔄 重新整理向量清單", self.refresh_chroma_memories_list, variant="secondary", padx=12, pady=5).pack(side="right")

        _switch_mem_tab("profile")

    # --- Memory Studio Event & Action Handlers ---
    def load_memory_page_data(self, silent: bool = True) -> None:
        # 1. Load user_profile.json
        prof_data = ConfigRepository.read_json(USER_PROFILE_FILE, [])
        if not isinstance(prof_data, list):
            prof_data = []
        self._profile_items_cache = [str(x) for x in prof_data]
        self._refresh_profile_listbox()

        # 2. Load memory.json
        short_mem = ConfigRepository.read_json(MEMORY_FILE, {})
        self.mem_short_text.delete("1.0", "end")
        self.mem_short_text.insert("1.0", json.dumps(short_mem, ensure_ascii=False, indent=2))

        # 3. Load base.json, favorability.json, reminders.json
        base_data = ConfigRepository.read_json(BASE_FILE, {})
        if not isinstance(base_data, dict):
            base_data = {}
        self.mem_master_id_var.set(str(base_data.get("master_id", "")))
        self.mem_notify_ch_var.set(str(base_data.get("notify_channel_id", "")))
        self.mem_admin_pwd_var.set(str(base_data.get("admin_password", "0000")))

        favor_data = ConfigRepository.read_json(FAVORABILITY_FILE, {})
        self.mem_favor_text.delete("1.0", "end")
        self.mem_favor_text.insert("1.0", json.dumps(favor_data, ensure_ascii=False, indent=2))

        rem_data = ConfigRepository.read_json(REMINDERS_FILE, [])
        self.mem_reminders_text.delete("1.0", "end")
        self.mem_reminders_text.insert("1.0", json.dumps(rem_data, ensure_ascii=False, indent=2))

        # 4. Load ChromaDB SQLite entries
        self.refresh_chroma_memories_list()

        if not silent:
            self.show_toast("✓ 已重新讀取所有記憶檔案與向量庫", level="info")

    def _refresh_profile_listbox(self) -> None:
        self.prof_listbox.delete(0, "end")
        for idx, item in enumerate(self._profile_items_cache):
            first_line = item.strip().splitlines()[0] if item.strip() else "(空白條目)"
            preview = first_line[:42] + ("…" if len(first_line) > 42 else "")
            self.prof_listbox.insert("end", f"[{idx + 1}] {preview}")
        if self._profile_items_cache:
            self.prof_listbox.selection_set(0)
            self._on_profile_listbox_select(None)
        else:
            self.prof_entry_editor.delete("1.0", "end")

    def _on_profile_listbox_select(self, _event: Optional[tk.Event]) -> None:  # type: ignore[type-arg]
        sel = self.prof_listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._profile_items_cache):
            self.prof_entry_editor.delete("1.0", "end")
            self.prof_entry_editor.insert("1.0", self._profile_items_cache[idx])

    def action_update_profile_entry(self) -> None:
        sel = self.prof_listbox.curselection()
        txt = self.prof_entry_editor.get("1.0", "end").strip()
        if not txt:
            messagebox.showwarning("內容為空", "請輸入備忘錄條目內容，或點選「刪除選取條目」。")
            return
        if not sel:
            self._profile_items_cache.append(txt)
        else:
            idx = int(sel[0])
            if 0 <= idx < len(self._profile_items_cache):
                self._profile_items_cache[idx] = txt
        ConfigRepository.write_json(USER_PROFILE_FILE, self._profile_items_cache)
        self._refresh_profile_listbox()
        self.show_toast("✓ 已更新並儲存主人長效備忘錄 (user_profile.json)", level="success")

    def action_add_profile_entry(self) -> None:
        txt = self.prof_entry_editor.get("1.0", "end").strip()
        if not txt:
            messagebox.showwarning("內容為空", "請先在右側編輯框輸入要新增的備忘錄內容。")
            return
        self._profile_items_cache.append(txt)
        ConfigRepository.write_json(USER_PROFILE_FILE, self._profile_items_cache)
        self._refresh_profile_listbox()
        self.prof_listbox.selection_clear(0, "end")
        self.prof_listbox.selection_set(len(self._profile_items_cache) - 1)
        self.show_toast("✓ 已新增一筆主人長效備忘錄條目", level="success")

    def action_delete_profile_entry(self) -> None:
        sel = self.prof_listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._profile_items_cache):
            del self._profile_items_cache[idx]
            ConfigRepository.write_json(USER_PROFILE_FILE, self._profile_items_cache)
            self._refresh_profile_listbox()
            self.show_toast("✓ 已刪除選取的長效備忘錄條目", level="info")

    def action_clear_short_term_memory(self) -> None:
        if not messagebox.askyesno("確認清空短期記憶", "確定要清空所有頻道的短期對話記憶 (data/memory.json) 嗎？"):
            return
        ConfigRepository.write_json(MEMORY_FILE, {})
        self.mem_short_text.delete("1.0", "end")
        self.mem_short_text.insert("1.0", "{}")
        self.show_toast("✓ 已清空所有頻道的短期對話記憶", level="info")

    def refresh_chroma_memories_list(self) -> None:
        self._chroma_rows_cache = ConfigRepository.read_chroma_memories()
        self.lbl_chroma_count.configure(text=f"目前向量記憶筆數：{len(self._chroma_rows_cache)} 筆")
        self.chroma_listbox.delete(0, "end")
        for idx, (emb_id, doc_txt) in enumerate(self._chroma_rows_cache):
            one_line = doc_txt.replace("\n", " ").strip()
            preview = one_line[:38] + ("…" if len(one_line) > 38 else "")
            self.chroma_listbox.insert("end", f"[{idx + 1}] {preview} ({emb_id[-8:]})")
        if self._chroma_rows_cache:
            self.chroma_listbox.selection_set(0)
            self._on_chroma_listbox_select(None)
        else:
            self.chroma_entry_editor.delete("1.0", "end")

    def _on_chroma_listbox_select(self, _event: Optional[tk.Event]) -> None:  # type: ignore[type-arg]
        sel = self.chroma_listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._chroma_rows_cache):
            _emb_id, doc_txt = self._chroma_rows_cache[idx]
            self.chroma_entry_editor.delete("1.0", "end")
            self.chroma_entry_editor.insert("1.0", doc_txt)

    def action_update_selected_chroma_memory(self) -> None:
        sel = self.chroma_listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if not (0 <= idx < len(self._chroma_rows_cache)):
            return
        emb_id, _old_txt = self._chroma_rows_cache[idx]
        new_txt = self.chroma_entry_editor.get("1.0", "end").strip()
        if not new_txt:
            messagebox.showwarning("內容為空", "向量記憶文字不可為空！")
            return
        try:
            if ConfigRepository.update_chroma_memory_text(emb_id, new_txt):
                self.refresh_chroma_memories_list()
                self.show_toast(f"✓ 已更新向量記憶 ({emb_id})", level="success")
            else:
                self.show_toast("⚠ 找不到對應的向量記憶 ID", level="warn")
        except Exception as e:
            messagebox.showerror("更新向量記憶失敗", f"寫入 chroma.sqlite3 時發生錯誤：\n{e}")

    def action_delete_selected_chroma_memory(self) -> None:
        sel = self.chroma_listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if not (0 <= idx < len(self._chroma_rows_cache)):
            return
        emb_id, _old_txt = self._chroma_rows_cache[idx]
        if not messagebox.askyesno("確認刪除向量記憶", f"確定要永久刪除這筆長期向量記憶 ({emb_id}) 嗎？"):
            return
        try:
            if ConfigRepository.delete_chroma_memory(emb_id):
                self.refresh_chroma_memories_list()
                self.show_toast(f"✓ 已刪除向量記憶 ({emb_id})", level="info")
        except Exception as e:
            messagebox.showerror("刪除向量記憶失敗", f"操作 chroma.sqlite3 時發生錯誤：\n{e}")

    def save_memory_page_data(self) -> None:
        try:
            short_obj = json.loads(self.mem_short_text.get("1.0", "end").strip() or "{}")
            favor_obj = json.loads(self.mem_favor_text.get("1.0", "end").strip() or "{}")
            rem_obj = json.loads(self.mem_reminders_text.get("1.0", "end").strip() or "[]")
        except Exception as e:
            messagebox.showerror("JSON 格式錯誤", f"請先確認短期記憶、好感度或提醒清單的 JSON 語法正確：\n{e}")
            return

        try:
            ConfigRepository.write_json(USER_PROFILE_FILE, self._profile_items_cache)
            ConfigRepository.write_json(MEMORY_FILE, short_obj)
            ConfigRepository.write_json(FAVORABILITY_FILE, favor_obj)
            ConfigRepository.write_json(REMINDERS_FILE, rem_obj)

            base_obj = ConfigRepository.read_json(BASE_FILE, {})
            if not isinstance(base_obj, dict):
                base_obj = {}
            master_id = self.mem_master_id_var.get().strip()
            notify_ch = self.mem_notify_ch_var.get().strip()
            admin_pwd = self.mem_admin_pwd_var.get().strip() or "0000"

            if master_id:
                base_obj["master_id"] = master_id
            else:
                base_obj.pop("master_id", None)

            if notify_ch:
                base_obj["notify_channel_id"] = notify_ch
            else:
                base_obj.pop("notify_channel_id", None)

            base_obj["admin_password"] = admin_pwd
            ConfigRepository.write_json(BASE_FILE, base_obj)

            self._refresh_dashboard_telemetry()
            self.show_toast("✓ 已儲存所有記憶、好感度、主人綁定與提醒設定", level="success")
        except Exception as e:
            messagebox.showerror("儲存記憶失敗", f"寫入記憶檔案時發生錯誤：\n{e}")

    def action_export_memory_bundle(self) -> None:
        default_name = ConfigRepository.generate_default_memory_filename(ROOT)
        dest_str = filedialog.asksaveasfilename(
            parent=self,
            title="匯出 Diana 完整記憶備份包 (.memory — 實質為 ZIP 格式)",
            defaultextension=".memory",
            initialfile=default_name,
            initialdir=str(ROOT),
            filetypes=[
                ("Diana 記憶備份包 (*.memory)", "*.memory"),
                ("ZIP 壓縮包 (*.zip)", "*.zip"),
                ("所有檔案 (*.*)", "*.*"),
            ],
        )
        if not dest_str:
            return
        try:
            # Save any pending edits in the UI before exporting
            self.save_memory_page_data()
            dest_path = Path(dest_str)
            if not dest_path.suffix:
                dest_path = dest_path.with_suffix(".memory")
            count = ConfigRepository.export_memory_bundle(dest_path)
            self.show_toast(f"✓ 記憶包匯出成功：{dest_path.name}（共 {count} 個檔案）", level="success")
            messagebox.showinfo(
                "匯出記憶成功",
                f"已成功將所有短期記憶、主人備忘錄、訪客關係與認知筆記、好感度、演化性格與 ChromaDB 向量庫打包匯出至：\n{dest_path}\n\n共包含 {count} 個檔案。",
            )
        except Exception as e:
            messagebox.showerror("匯出記憶失敗", f"打包記憶備份時發生錯誤：\n{e}")

    def action_import_memory_bundle(self) -> None:
        src_str = filedialog.askopenfilename(
            parent=self,
            title="選擇要匯入還原的 Diana 記憶備份包 (.memory)",
            initialdir=str(ROOT),
            filetypes=[
                ("Diana 記憶備份包 (*.memory)", "*.memory"),
                ("相容記憶壓縮包 (*.memory;*.zip)", "*.memory *.zip"),
                ("所有檔案 (*.*)", "*.*"),
            ],
        )
        if not src_str:
            return

        if self.workers.is_bot_alive():
            if not messagebox.askyesno(
                "匯入前需先停止 Diana Bot",
                "偵測到 Diana Bot 目前正在運行中。\n為避免 ChromaDB 向量資料庫鎖定衝突，匯入記憶包前將先自動停止 Bot，是否繼續？",
            ):
                return
            self.workers.stop_bot_process()
            time.sleep(0.8)

        if not messagebox.askyesno(
            "確認匯入並覆蓋目前記憶",
            f"即將從以下 .memory 備份包還原記憶：\n{src_str}\n\n這將會覆蓋目前的短期對話記憶、長效備忘錄、好感度與 ChromaDB 向量記憶庫，確定要繼續嗎？",
        ):
            return

        try:
            restored = ConfigRepository.import_memory_bundle(Path(src_str))
            self.load_memory_page_data(silent=True)
            self.load_current_persona_file(silent=True)
            self._refresh_dashboard_telemetry()
            self.show_toast(f"✓ 記憶包匯入完成（共還原 {restored} 個檔案）", level="success")
            messagebox.showinfo("匯入記憶成功", f"已成功還原 {restored} 個記憶與向量庫檔案！\n介面資料已自動刷新。")
        except Exception as e:
            messagebox.showerror("匯入記憶失敗", f"還原記憶備份包時發生錯誤：\n{e}")

    # -----------------------------------------------------------------------
    # Page 4: Responsive System Diagnostics, Parameters & Evolution Control
    # -----------------------------------------------------------------------
    def _build_system_page(self) -> None:
        p = self.pages["system"]
        scroll_host = ResponsiveScrollFrame(p, bg=PALETTE["bg_app"])
        scroll_host.pack(fill="both", expand=True)
        body = scroll_host.content

        # Card 1: Environment & File Health Checklist
        diag_card = self._create_card(
            body,
            title="系統必要檔案與環境健檢 (System Health Check)",
            subtitle="自動驗證 Python 版本、Discord / ChromaDB 套件、設定檔與參考圖資源（支援視窗寬度自適應排版）",
        )
        diag_card.pack(fill="x", pady=(0, 12))

        self.diag_grid = tk.Frame(diag_card, bg=PALETTE["bg_card"])
        self.diag_grid.pack(fill="x", pady=(0, 10))
        self.diag_cells: List[tk.Frame] = []

        diag_btn_row = tk.Frame(diag_card, bg=PALETTE["bg_card"])
        diag_btn_row.pack(fill="x")

        self.btn_install_deps = ModernButton(
            diag_btn_row,
            "📦 安裝依賴",
            self.action_install_deps,
            variant="primary",
        )
        self.btn_install_deps.pack(side="left", padx=(0, 8))

        ModernButton(
            diag_btn_row,
            "🔄 重新檢查檔案狀態",
            self.refresh_file_diagnostics,
            variant="secondary",
        ).pack(side="left", padx=8)

        ModernButton(
            diag_btn_row,
            "📂 開啟專案根目錄",
            self.open_project_folder,
            variant="secondary",
        ).pack(side="left", padx=8)

        # Card 2: LLM Reasoning & Generation Parameters
        param_card = self._create_card(
            body,
            title="模型推理參數與自主演化控制 (Parameters & Evolution)",
            subtitle="管理 Temperature、Thinking Tokens，或立即觸發離線演化引擎",
        )
        param_card.pack(fill="x", pady=(0, 12))

        self._create_labeled_entry(param_card, "Temperature (創造力)", self.vars["temperature"], "建議範圍 0.5 ~ 1.0 (預設 0.75)")
        self._create_labeled_entry(param_card, "Thinking Tokens", self.vars["thinking_tokens"], "最大思考 Token 上限 (預設 256)")

        param_actions = tk.Frame(param_card, bg=PALETTE["bg_card"])
        param_actions.pack(fill="x", pady=(12, 0))

        self.btn_run_evolution = ModernButton(
            param_actions,
            "🧬 立即執行一次演化 (diana_evolution.py --run-now)",
            self.action_run_evolution_now,
            variant="secondary",
        )
        self.btn_run_evolution.pack(side="left")

        ModernButton(
            param_actions,
            "💾 儲存推理參數",
            self.save_all_settings,
            variant="primary",
        ).pack(side="right")

        # Card 3: Cold-Install Agent & Memory-Free .seed Packager
        seed_card = self._create_card(
            body,
            title="冷安裝部署代理與無記憶種子包封裝 (Cold-Install & .seed Agent)",
            subtitle="管理零外部依賴 C# 原生安裝程式 (DianaSeedInstaller.exe) 與無記憶系統包 (.seed)，支援自動配置 Python 3.12 與 Ollama (gemma4:e2b)",
        )
        seed_card.pack(fill="x", pady=(0, 12))

        seed_actions = tk.Frame(seed_card, bg=PALETTE["bg_card"])
        seed_actions.pack(fill="x", pady=(4, 0))

        self.btn_pack_seed = ModernButton(
            seed_actions,
            "🌱 立即封裝無記憶 .seed 系統包 (Diana-Bot.seed)",
            self.action_pack_clean_seed,
            variant="primary",
        )
        self.btn_pack_seed.pack(side="left", padx=(0, 8))

        ModernButton(
            seed_actions,
            "🚀 開啟原生冷安裝部署代理 (DianaSeedInstaller.exe)",
            self.action_open_seed_installer,
            variant="secondary",
        ).pack(side="left", padx=8)

    def refresh_file_diagnostics(self) -> None:
        for child in self.diag_grid.winfo_children():
            child.destroy()
        self.diag_cells.clear()

        ref_count = len(list(REFERENCE_IMG_DIR.glob("*.*"))) if REFERENCE_IMG_DIR.exists() else 0
        checks = [
            (
                "本地 Python 版本",
                True,
                f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            ),
            ("Discord 套件", importlib.util.find_spec("discord") is not None, "discord.py"),
            ("ChromaDB 套件", importlib.util.find_spec("chromadb") is not None, "chromadb"),
            ("核心主程式 (bot.py)", (ROOT / "bot.py").exists(), "bot.py"),
            ("附帶功能模組 (diana_addons.py)", (ROOT / "diana_addons.py").exists(), "diana_addons.py"),
            ("演化引擎 (diana_evolution.py)", (ROOT / "diana_evolution.py").exists(), "diana_evolution.py"),
            ("角色設定檔 (character_persona.json)", PERSONA.exists(), "config/character_persona.json"),
            ("Discord Bot Token", bool(ConfigRepository.read_first_line(TOKEN)), "config/discord_token.txt"),
            ("Chat API Key (聊天專用)", bool(ConfigRepository.read_first_line(CHAT_KEY)), "config/google_api_key_chat.txt"),
            ("Evolution API Key (演化專用)", bool(ConfigRepository.read_first_line(EVOLUTION_KEY)), "config/google_api_key_evolution.txt"),
            ("Image API Key (生圖專用)", bool(ConfigRepository.read_first_line(IMAGE_KEY)), "config/google_api_key_image.txt"),
            (f"角色參考圖庫 ({ref_count} 張)", ref_count > 0, "img/reference/"),
            ("原生冷安裝代理 (DianaSeedInstaller.exe)", SEED_INSTALLER_EXE.exists(), "DianaSeedInstaller.exe"),
            ("無記憶系統種子包 (Diana-Bot.seed)", DEFAULT_SEED_FILE.exists(), "Diana-Bot.seed"),
        ]

        d_cols = 2 if self.viewport.dual_col_forms else 1
        for c in range(2):
            self.diag_grid.columnconfigure(c, weight=1 if c < d_cols else 0, uniform="diag" if c < d_cols else "")

        for idx, (label, ok, path_hint) in enumerate(checks):
            r, c = divmod(idx, d_cols)
            cell = tk.Frame(
                self.diag_grid,
                bg=PALETTE["bg_input"],
                highlightthickness=1,
                highlightbackground=PALETTE["border"],
                padx=12,
                pady=8,
            )
            cell.grid(row=r, column=c, sticky="ew", padx=4, pady=4)
            badge_fg = PALETTE["success"] if ok else PALETTE["warning"]
            badge_txt = "✓ 目前執行" if label == "本地 Python 版本" else ("✓ 已就緒" if ok else "⚠ 待設定")
            tk.Label(cell, text=badge_txt, bg=PALETTE["bg_input"], fg=badge_fg, font=("Microsoft JhengHei UI", 9, "bold")).pack(side="left", padx=(0, 10))
            tk.Label(cell, text=label, bg=PALETTE["bg_input"], fg=PALETTE["text_primary"], font=("Microsoft JhengHei UI", 9, "bold")).pack(side="left")
            tk.Label(cell, text=path_hint, bg=PALETTE["bg_input"], fg=PALETTE["text_muted"], font=("Consolas", 8)).pack(side="right")
            self.diag_cells.append(cell)

    # -----------------------------------------------------------------------
    # Settings & Persona Actions (Orchestration Layer)
    # -----------------------------------------------------------------------
    def load_all_settings(self) -> None:
        self.cfg = ConfigRepository.load_gui_config()
        for k in ["local_base_url", "local_model", "web_base_url", "web_model", "image_endpoint", "image_model"]:
            self.vars[k].set(str(self.cfg.get(k, DEFAULT_CFG[k])))

        local_m = str(self.cfg.get("local_model", DEFAULT_CFG["local_model"]))
        web_m = str(self.cfg.get("web_model", DEFAULT_CFG["web_model"]))
        master_m = str(self.cfg.get("master_model") or local_m)
        guest_m = str(self.cfg.get("guest_model") or local_m)
        master_b = str(self.cfg.get("master_backend") or ("web" if (master_m == web_m or "gemini" in master_m.lower()) else "local"))
        guest_b = str(self.cfg.get("guest_backend") or ("web" if (guest_m == web_m or "gemini" in guest_m.lower()) else "local"))

        self.vars["master_model"].set(master_m)
        self.vars["guest_model"].set(guest_m)
        self.vars["master_backend"].set(master_b)
        self.vars["guest_backend"].set(guest_b)

        self.vars["discord"].set(ConfigRepository.read_first_line(TOKEN))
        self.vars["chat_key"].set(ConfigRepository.read_first_line(CHAT_KEY))
        self.vars["evolution_key"].set(ConfigRepository.read_first_line(EVOLUTION_KEY))
        self.vars["evolution_model"].set(ConfigRepository.read_second_line(EVOLUTION_KEY))
        self.vars["image_key"].set(ConfigRepository.read_first_line(IMAGE_KEY))
        self.vars["temperature"].set(str(self.cfg.get("temperature", 0.75)))
        self.vars["thinking_tokens"].set(str(self.cfg.get("thinking_tokens", 256)))

        self.load_current_persona_file(silent=True)
        self.load_memory_page_data(silent=True)
        self.refresh_file_diagnostics()
        self._refresh_dashboard_telemetry()

    def save_all_settings(self, silent: bool = False) -> bool:
        try:
            for k in [
                "local_base_url",
                "local_model",
                "web_base_url",
                "web_model",
                "master_backend",
                "master_model",
                "guest_backend",
                "guest_model",
                "image_endpoint",
                "image_model",
            ]:
                if k in self.vars and self.vars[k].get().strip():
                    self.cfg[k] = self.vars[k].get().strip()
            self.cfg["temperature"] = float(self.vars["temperature"].get().strip() or 0.75)
            self.cfg["thinking_tokens"] = int(float(self.vars["thinking_tokens"].get().strip() or 256))
            ConfigRepository.write_json(GUI_CONFIG, self.cfg)

            ConfigRepository.write_first_line_preserve_rest(TOKEN, self.vars["discord"].get())
            ConfigRepository.write_first_line_preserve_rest(CHAT_KEY, self.vars["chat_key"].get())
            ConfigRepository.write_first_line_preserve_rest(
                EVOLUTION_KEY,
                self.vars["evolution_key"].get(),
                second_line_override=self.vars["evolution_model"].get(),
            )
            ConfigRepository.write_first_line_preserve_rest(IMAGE_KEY, self.vars["image_key"].get())

            self.refresh_file_diagnostics()
            self._refresh_dashboard_telemetry()
            if not silent:
                self.show_toast("✓ 連線設定、推理參數與獨立憑證已成功儲存", level="success")
            return True
        except Exception as e:
            messagebox.showerror("儲存設定失敗", f"請檢查數值格式是否正確：\n{e}")
            return False

    def load_current_persona_file(self, silent: bool = False) -> None:
        obj = ConfigRepository.load_active_persona()
        self._set_persona_object(obj)
        if not silent:
            self.show_toast("✓ 已載入目前使用中的 config/character_persona.json", level="info")

    def load_diana_example(self) -> None:
        obj = ConfigRepository.load_diana_example_persona()
        self._set_persona_object(obj)
        self.show_toast("✓ 已載入黛安娜預設範例（點擊「儲存 Persona 設定」即可套用）", level="info")

    def load_blank_persona(self) -> None:
        obj = ConfigRepository.blank_persona_template()
        self._set_persona_object(obj)
        self.show_toast("✓ 已載入空白角色模板", level="info")

    def _get_current_editor_persona_dict(self) -> Dict[str, Any]:
        if self.persona_mode == "visual":
            obj = self._collect_visual_form_to_dict()
            self.persona_text.delete("1.0", "end")
            self.persona_text.insert("1.0", json.dumps(obj, ensure_ascii=False, indent=2))
            return obj
        obj = json.loads(self.persona_text.get("1.0", "end"))
        if not isinstance(obj, dict):
            raise ValueError("Persona 內容必須為 JSON 物件 (Object)")
        self._sync_json_to_visual_form(obj)
        return obj

    def save_persona(self) -> None:
        try:
            obj = self._get_current_editor_persona_dict()
            ConfigRepository.write_json(PERSONA, obj)
            self._refresh_dashboard_telemetry()
            self.show_toast(f"✓ 角色「{obj.get('name', '未命名')}」已儲存至 config/character_persona.json", level="success")
        except Exception as e:
            messagebox.showerror("Persona 儲存失敗", f"JSON 格式無效或寫入失敗：\n{e}")

    def action_export_persona_file(self, parent_win: Optional[tk.Misc] = None) -> Optional[Path]:
        """
        Exports the current persona as a `.persona` file (which is JSON under the hood)
        inside `config/` (or user-chosen path) with custom user naming.
        """
        try:
            obj = self._get_current_editor_persona_dict()
        except Exception:
            obj = ConfigRepository.load_active_persona()

        raw_name = str(obj.get("name", "黛安娜")).strip() or "黛安娜"
        safe_name = re.sub(r'[<>:"/\\|?*]+', "_", raw_name)
        dest_str = filedialog.asksaveasfilename(
            parent=parent_win or self,
            title="匯出角色設定檔 (.persona — 實質為 JSON 格式，預設存放於 config/ 供快速切換)",
            defaultextension=".persona",
            initialdir=str(CONFIG),
            initialfile=f"{safe_name}.persona",
            filetypes=[
                ("Diana 角色設定檔 (*.persona)", "*.persona"),
                ("JSON 檔案 (*.json)", "*.json"),
                ("所有檔案 (*.*)", "*.*"),
            ],
        )
        if not dest_str:
            return None
        try:
            dest_path = Path(dest_str)
            if not dest_path.suffix:
                dest_path = dest_path.with_suffix(".persona")
            ConfigRepository.write_json(dest_path, obj)
            self._refresh_dashboard_telemetry()
            self.show_toast(f"✓ 已匯出角色檔：{dest_path.name}", level="success")
            return dest_path
        except Exception as e:
            messagebox.showerror("匯出角色檔失敗", f"寫入 .persona 角色檔時發生錯誤：\n{e}", parent=parent_win or self)
            return None

    # -----------------------------------------------------------------------
    # Dashboard Quick-Switch & Quick-View Modals (Persona / Relationships / Schedules / Engine)
    # -----------------------------------------------------------------------
    def _open_persona_quick_switch_dialog(self) -> None:
        """
        Scans ONLY `config/*.persona` files and lets the user quickly switch personas.
        Prompts a confirmation dialog warning that `config/character_persona.json` will be overwritten.
        """
        win = tk.Toplevel(self)
        win.title("🎭 快速切換角色設定檔 (僅偵測 config/*.persona)")
        win.geometry("820x520")
        win.minsize(680, 420)
        win.configure(bg=PALETTE["bg_app"])
        win.transient(self)

        hdr = tk.Frame(win, bg=PALETTE["bg_app"], padx=18, pady=12)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text="🎭 快速切換目前角色 (config/*.persona)",
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 13, "bold"),
        ).pack(side="left")
        tk.Label(
            hdr,
            text="僅掃描 config 資料夾內副檔名為 .persona 之 JSON 角色檔",
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9),
        ).pack(side="left", padx=(12, 0))

        body = tk.Frame(win, bg=PALETTE["bg_card"], highlightthickness=1, highlightbackground=PALETTE["border"], padx=14, pady=12)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        body.columnconfigure(0, weight=2)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(1, weight=1)

        tk.Label(
            body,
            text="📂 config/ 內偵測到的 .persona 角色檔：",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 10), pady=(0, 6))

        tk.Label(
            body,
            text="🔍 所選角色設定預覽 (JSON 內容摘要)：",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="ew", pady=(0, 6))

        lb = tk.Listbox(
            body,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            selectbackground=PALETTE["accent"],
            selectforeground="#FFFFFF",
            font=("Microsoft JhengHei UI", 10),
            relief="flat",
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            exportselection=False,
        )
        lb.grid(row=1, column=0, sticky="nsew", padx=(0, 10))

        preview = scrolledtext.ScrolledText(
            body,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            font=("Consolas", 9),
            relief="flat",
            padx=10,
            pady=8,
            wrap="word",
        )
        preview.grid(row=1, column=1, sticky="nsew")

        persona_entries: List[Tuple[Path, Dict[str, Any]]] = []

        def _reload_list() -> None:
            nonlocal persona_entries
            persona_entries = ConfigRepository.list_persona_files()
            lb.delete(0, "end")
            active_name = str(ConfigRepository.load_active_persona().get("name", "")).strip()
            for p_path, p_data in persona_entries:
                p_name = str(p_data.get("name", p_path.stem)).strip() or p_path.stem
                p_age = str(p_data.get("age_style", "")).strip()
                active_tag = " [目前同名]" if p_name == active_name else ""
                lb.insert("end", f"🎭 {p_path.name}  —  {p_name} ({p_age}){active_tag}")
            if persona_entries:
                lb.selection_set(0)
                _on_select(None)
            else:
                preview.configure(state="normal")
                preview.delete("1.0", "end")
                preview.insert("1.0", "目前 config/ 資料夾內尚無 .persona 檔案。\n您可以點擊下方「📤 將當前角色匯出為 .persona」立即建立！")
                preview.configure(state="disabled")

        def _on_select(_e: Any) -> None:
            sel = lb.curselection()
            if not sel or sel[0] >= len(persona_entries):
                return
            p_path, p_data = persona_entries[int(sel[0])]
            summary_lines = [
                f"檔案路徑：config/{p_path.name}",
                f"角色名稱：{p_data.get('name', '未命名')}",
                f"年齡風格：{p_data.get('age_style', '未設定')}",
                f"角色背景：{p_data.get('background', '')}",
                f"主人關係：{p_data.get('relationship', '')}",
                "-" * 46,
                json.dumps(p_data, ensure_ascii=False, indent=2),
            ]
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            preview.insert("1.0", "\n".join(summary_lines))
            preview.configure(state="disabled")

        lb.bind("<<ListboxSelect>>", _on_select)

        def _apply_selected() -> None:
            sel = lb.curselection()
            if not sel or sel[0] >= len(persona_entries):
                messagebox.showwarning("請選擇角色檔", "請先從左側清單選擇一個 .persona 角色檔。", parent=win)
                return
            p_path, p_data = persona_entries[int(sel[0])]
            p_name = str(p_data.get("name", p_path.stem)).strip() or p_path.stem
            cur_name = str(ConfigRepository.load_active_persona().get("name", "未命名")).strip()

            if not messagebox.askyesno(
                "確認覆蓋當前角色 (請確認是否已備份)",
                f"⚠️ 即將載入角色檔「{p_path.name} ({p_name})」並覆寫當前的 config/character_persona.json！\n\n"
                f"目前使用中的角色為：「{cur_name}」\n"
                f"載入後將會直接覆蓋當前角色設定，請確認您是否已將當前角色匯出備份 (.persona)？\n\n"
                f"確定要繼續載入並覆蓋嗎？",
                parent=win,
            ):
                return

            try:
                ConfigRepository.write_json(PERSONA, p_data)
                self._set_persona_object(p_data)
                self._refresh_dashboard_telemetry()
                self.show_toast(f"✓ 已切換角色為「{p_name}」({p_path.name})", level="success")
                win.destroy()
            except Exception as e:
                messagebox.showerror("切換角色失敗", f"覆寫 character_persona.json 時發生錯誤：\n{e}", parent=win)

        def _export_and_refresh() -> None:
            exported = self.action_export_persona_file(parent_win=win)
            if exported:
                _reload_list()

        lb.bind("<Double-Button-1>", lambda _e: _apply_selected())
        _reload_list()

        footer = tk.Frame(win, bg=PALETTE["bg_app"], padx=18, pady=10)
        footer.pack(fill="x")
        ModernButton(footer, "📤 將當前角色匯出為 .persona", _export_and_refresh, variant="secondary", padx=12, pady=6).pack(side="left")
        ModernButton(footer, "🔄 重新掃描 config/", _reload_list, variant="ghost", padx=10, pady=6).pack(side="left", padx=6)
        ModernButton(footer, "✅ 載入並覆蓋當前角色", _apply_selected, variant="primary", padx=18, pady=6).pack(side="right")
        ModernButton(footer, "取消", win.destroy, variant="secondary", padx=12, pady=6).pack(side="right", padx=8)

    def _open_relationships_quick_view_dialog(self) -> None:
        """
        Displays ONLY non-master guests, showing each guest's:
        1) Simple relationship & favorability
        2) Event memories
        3) Object cognition notes (editable & persistent in data/guest_notes.json)
        """
        win = tk.Toplevel(self)
        win.title("👥 訪客關係、事件記憶與對象認知筆記 (僅顯示主人以外的訪客)")
        win.geometry("920x580")
        win.minsize(740, 460)
        win.configure(bg=PALETTE["bg_app"])
        win.transient(self)

        base_data = ConfigRepository.read_json(BASE_FILE, {})
        master_id = str(base_data.get("master_id", "")).strip() if isinstance(base_data, dict) else ""

        hdr = tk.Frame(win, bg=PALETTE["bg_app"], padx=18, pady=12)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text="👥 訪客關係與對象認知筆記快速檢視",
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 13, "bold"),
        ).pack(side="left")
        tk.Label(
            hdr,
            text=f"（已自動排除主人 ID: {master_id or '未綁定'}，僅顯示訪客互動關係、事件記憶與認知筆記）",
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9),
        ).pack(side="left", padx=(10, 0))

        body = tk.Frame(win, bg=PALETTE["bg_card"], highlightthickness=1, highlightbackground=PALETTE["border"], padx=14, pady=12)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        body.columnconfigure(0, weight=2)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(0, weight=1)

        left_col = tk.Frame(body, bg=PALETTE["bg_card"])
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        tk.Label(
            left_col,
            text="📋 訪客清單 (點擊檢視/編輯)：",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        lb = tk.Listbox(
            left_col,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            selectbackground=PALETTE["accent"],
            selectforeground="#FFFFFF",
            font=("Microsoft JhengHei UI", 10),
            relief="flat",
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            exportselection=False,
        )
        lb.pack(fill="both", expand=True)

        right_col = tk.Frame(body, bg=PALETTE["bg_card"])
        right_col.grid(row=0, column=1, sticky="nsew")

        form_top = tk.Frame(right_col, bg=PALETTE["bg_card"])
        form_top.pack(fill="x", pady=(0, 8))
        form_top.columnconfigure(1, weight=1)
        form_top.columnconfigure(3, weight=1)

        g_name_var = tk.StringVar()
        g_uid_var = tk.StringVar()
        g_rel_var = tk.StringVar()
        g_fav_var = tk.StringVar(value="50")

        tk.Label(form_top, text="訪客名稱:", bg=PALETTE["bg_card"], fg=PALETTE["text_primary"], font=("Microsoft JhengHei UI", 9, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        tk.Entry(form_top, textvariable=g_name_var, bg=PALETTE["bg_input"], fg=PALETTE["text_primary"], insertbackground="#FFFFFF", relief="flat", font=("Microsoft JhengHei UI", 9)).grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=3, ipady=3)

        tk.Label(form_top, text="識別 ID:", bg=PALETTE["bg_card"], fg=PALETTE["text_primary"], font=("Microsoft JhengHei UI", 9, "bold")).grid(row=0, column=2, sticky="w", padx=(0, 6), pady=3)
        tk.Entry(form_top, textvariable=g_uid_var, bg=PALETTE["bg_input"], fg=PALETTE["text_primary"], insertbackground="#FFFFFF", relief="flat", font=("Consolas", 9)).grid(row=0, column=3, sticky="ew", pady=3, ipady=3)

        tk.Label(form_top, text="簡單關係:", bg=PALETTE["bg_card"], fg=PALETTE["text_primary"], font=("Microsoft JhengHei UI", 9, "bold")).grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
        tk.Entry(form_top, textvariable=g_rel_var, bg=PALETTE["bg_input"], fg=PALETTE["text_primary"], insertbackground="#FFFFFF", relief="flat", font=("Microsoft JhengHei UI", 9)).grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=3, ipady=3)

        tk.Label(form_top, text="好感度 (0-100):", bg=PALETTE["bg_card"], fg=PALETTE["text_primary"], font=("Microsoft JhengHei UI", 9, "bold")).grid(row=1, column=2, sticky="w", padx=(0, 6), pady=3)
        tk.Entry(form_top, textvariable=g_fav_var, bg=PALETTE["bg_input"], fg=PALETTE["text_primary"], insertbackground="#FFFFFF", relief="flat", font=("Consolas", 9)).grid(row=1, column=3, sticky="ew", pady=3, ipady=3)

        tk.Label(
            right_col,
            text="🧠 對象認知筆記 (黛安娜對這位訪客的印象、特徵與應對備忘)：",
            bg=PALETTE["bg_card"],
            fg="#A5B4FC",
            font=("Microsoft JhengHei UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(4, 3))
        txt_note = scrolledtext.ScrolledText(
            right_col,
            height=5,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            insertbackground="#FFFFFF",
            font=("Microsoft JhengHei UI", 9),
            relief="flat",
            padx=8,
            pady=6,
            wrap="word",
        )
        txt_note.pack(fill="x", pady=(0, 8))

        tk.Label(
            right_col,
            text="📅 與該訪客的事件記憶 (每行一筆事件，對話時會自動記錄最新互動)：",
            bg=PALETTE["bg_card"],
            fg=PALETTE["cyan"],
            font=("Microsoft JhengHei UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(2, 3))
        txt_events = scrolledtext.ScrolledText(
            right_col,
            height=7,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            insertbackground="#FFFFFF",
            font=("Microsoft JhengHei UI", 9),
            relief="flat",
            padx=8,
            pady=6,
            wrap="word",
        )
        txt_events.pack(fill="both", expand=True)

        guests_cache: List[Dict[str, Any]] = []

        def _render_list(select_idx: int = 0) -> None:
            lb.delete(0, "end")
            for g in guests_cache:
                lb.insert("end", f"👤 {g['name']}  |  {g['relationship']} (好感 {g['favorability']})")
            if guests_cache:
                idx = min(max(select_idx, 0), len(guests_cache) - 1)
                lb.selection_set(idx)
                _on_select(None)
            else:
                g_name_var.set("")
                g_uid_var.set("")
                g_rel_var.set("一般訪客 / 認識的客人")
                g_fav_var.set("50")
                txt_note.delete("1.0", "end")
                txt_events.delete("1.0", "end")

        def _on_select(_e: Any) -> None:
            sel = lb.curselection()
            if not sel or sel[0] >= len(guests_cache):
                return
            g = guests_cache[int(sel[0])]
            g_name_var.set(str(g.get("name", "")))
            g_uid_var.set(str(g.get("user_id", "")))
            g_rel_var.set(str(g.get("relationship", "一般訪客")))
            g_fav_var.set(str(g.get("favorability", 50)))
            txt_note.delete("1.0", "end")
            txt_note.insert("1.0", str(g.get("cognition_note", "")))
            txt_events.delete("1.0", "end")
            txt_events.insert("1.0", "\n".join(g.get("events", [])))

        lb.bind("<<ListboxSelect>>", _on_select)

        def _save_current_guest() -> None:
            name = g_name_var.get().strip()
            uid = g_uid_var.get().strip() or name
            if not name:
                messagebox.showwarning("請填寫訪客名稱", "請輸入訪客名稱再儲存。", parent=win)
                return
            if master_id and uid == master_id:
                messagebox.showwarning("不可加入主人", "關係列表僅顯示主人以外的訪客，請勿填入主人的 Discord ID。", parent=win)
                return
            try:
                fav = max(0, min(100, int(float(g_fav_var.get().strip() or 50))))
            except Exception:
                fav = 50
            rel = g_rel_var.get().strip() or "一般訪客 / 認識的客人"
            note = txt_note.get("1.0", "end").strip()
            evts = [line.strip() for line in txt_events.get("1.0", "end").splitlines() if line.strip()]

            sel = lb.curselection()
            entry = {
                "key": uid or name,
                "user_id": uid or name,
                "name": name,
                "favorability": fav,
                "relationship": rel,
                "cognition_note": note,
                "events": evts,
                "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            if sel and sel[0] < len(guests_cache):
                idx = int(sel[0])
                guests_cache[idx] = entry
            else:
                guests_cache.append(entry)
                idx = len(guests_cache) - 1

            ConfigRepository.save_guest_relationships(guests_cache)
            self.load_memory_page_data(silent=True)
            self._refresh_dashboard_telemetry()
            _render_list(idx)
            self.show_toast(f"✓ 已儲存訪客「{name}」的關係、事件記憶與認知筆記", level="success")

        def _add_new_guest() -> None:
            new_g = {
                "key": f"guest_{len(guests_cache) + 1}",
                "user_id": "",
                "name": f"新訪客 {len(guests_cache) + 1}",
                "favorability": 50,
                "relationship": "一般訪客 / 認識的客人",
                "cognition_note": "對主人保持禮貌的訪客",
                "events": [],
                "last_seen": "",
            }
            guests_cache.append(new_g)
            _render_list(len(guests_cache) - 1)

        def _delete_selected_guest() -> None:
            sel = lb.curselection()
            if not sel or sel[0] >= len(guests_cache):
                return
            idx = int(sel[0])
            g_name = guests_cache[idx].get("name", "訪客")
            if not messagebox.askyesno("確認刪除訪客紀錄", f"確定要移除訪客「{g_name}」的關係與認知筆記嗎？", parent=win):
                return
            del guests_cache[idx]
            ConfigRepository.save_guest_relationships(guests_cache)
            self._refresh_dashboard_telemetry()
            _render_list(max(0, idx - 1))
            self.show_toast(f"✓ 已移除訪客「{g_name}」", level="info")

        guests_cache = ConfigRepository.collect_guest_relationships()
        _render_list(0)

        footer = tk.Frame(win, bg=PALETTE["bg_app"], padx=18, pady=10)
        footer.pack(fill="x")
        ModernButton(footer, "➕ 新增訪客紀錄", _add_new_guest, variant="secondary", padx=12, pady=6).pack(side="left")
        ModernButton(footer, "🗑️ 刪除選取訪客", _delete_selected_guest, variant="danger", padx=12, pady=6).pack(side="left", padx=8)
        ModernButton(footer, "💾 儲存當前訪客關係與筆記", _save_current_guest, variant="primary", padx=16, pady=6).pack(side="right")
        ModernButton(footer, "關閉", win.destroy, variant="secondary", padx=12, pady=6).pack(side="right", padx=8)

    def _open_schedules_quick_view_dialog(self) -> None:
        """
        Quick-view & quick-manage modal for active schedules and reminders (`data/reminders.json`).
        """
        win = tk.Toplevel(self)
        win.title("⏰ 目前排程與提醒快速檢視 (data/reminders.json)")
        win.geometry("820x500")
        win.minsize(660, 400)
        win.configure(bg=PALETTE["bg_app"])
        win.transient(self)

        hdr = tk.Frame(win, bg=PALETTE["bg_app"], padx=18, pady=12)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text="⏰ 目前排程與提醒事項快速檢視",
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 13, "bold"),
        ).pack(side="left")
        tk.Label(
            hdr,
            text="直接查看、新增或移除 data/reminders.json 內的排程項目",
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9),
        ).pack(side="left", padx=(10, 0))

        body = tk.Frame(win, bg=PALETTE["bg_card"], highlightthickness=1, highlightbackground=PALETTE["border"], padx=14, pady=12)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        lb = tk.Listbox(
            body,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            selectbackground=PALETTE["accent"],
            selectforeground="#FFFFFF",
            font=("Consolas", 10),
            relief="flat",
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            height=11,
            exportselection=False,
        )
        lb.pack(fill="both", expand=True, pady=(0, 10))

        add_frame = tk.Frame(body, bg=PALETTE["bg_card"])
        add_frame.pack(fill="x")
        tk.Label(add_frame, text="🕒 提醒時間 (YYYY-MM-DD HH:MM):", bg=PALETTE["bg_card"], fg=PALETTE["text_secondary"], font=("Microsoft JhengHei UI", 9, "bold")).pack(side="left")
        time_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d %H:%M"))
        tk.Entry(add_frame, textvariable=time_var, width=18, bg=PALETTE["bg_input"], fg=PALETTE["text_primary"], insertbackground="#FFFFFF", relief="flat", font=("Consolas", 9)).pack(side="left", padx=(6, 12), ipady=3)

        tk.Label(add_frame, text="📝 排程提醒內容:", bg=PALETTE["bg_card"], fg=PALETTE["text_secondary"], font=("Microsoft JhengHei UI", 9, "bold")).pack(side="left")
        content_var = tk.StringVar()
        tk.Entry(add_frame, textvariable=content_var, bg=PALETTE["bg_input"], fg=PALETTE["text_primary"], insertbackground="#FFFFFF", relief="flat", font=("Microsoft JhengHei UI", 9)).pack(side="left", fill="x", expand=True, padx=6, ipady=3)

        reminders_list: List[Any] = []

        def _reload_schedules() -> None:
            nonlocal reminders_list
            raw = ConfigRepository.read_json(REMINDERS_FILE, [])
            reminders_list = raw if isinstance(raw, list) else []
            lb.delete(0, "end")
            if not reminders_list:
                lb.insert("end", "（目前尚無任何排程或提醒事項）")
                return
            for idx, item in enumerate(reminders_list):
                if isinstance(item, dict):
                    t_s = str(item.get("time", item.get("datetime", "未指定時間")))
                    c_s = str(item.get("content", item.get("task", item.get("message", ""))))
                    ch_s = str(item.get("channel_id", item.get("user_id", "")))
                    ch_tag = f" [頻道/對象: {ch_s}]" if ch_s else ""
                    lb.insert("end", f"[{idx + 1:02d}] ⏰ {t_s}  —  {c_s}{ch_tag}")
                else:
                    lb.insert("end", f"[{idx + 1:02d}] ⏰ {item}")

        def _add_schedule() -> None:
            t_s = time_var.get().strip()
            c_s = content_var.get().strip()
            if not t_s or not c_s:
                messagebox.showwarning("欄位未填", "請填寫提醒時間與排程內容。", parent=win)
                return
            base_d = ConfigRepository.read_json(BASE_FILE, {})
            default_ch = base_d.get("notify_channel_id", "") if isinstance(base_d, dict) else ""
            reminders_list.append({
                "time": t_s,
                "content": c_s,
                "channel_id": str(default_ch),
            })
            ConfigRepository.write_json(REMINDERS_FILE, reminders_list)
            content_var.set("")
            self.load_memory_page_data(silent=True)
            self._refresh_dashboard_telemetry()
            _reload_schedules()
            self.show_toast("✓ 已新增一筆排程提醒", level="success")

        def _delete_schedule() -> None:
            sel = lb.curselection()
            if not sel or not reminders_list or sel[0] >= len(reminders_list):
                return
            idx = int(sel[0])
            del reminders_list[idx]
            ConfigRepository.write_json(REMINDERS_FILE, reminders_list)
            self.load_memory_page_data(silent=True)
            self._refresh_dashboard_telemetry()
            _reload_schedules()
            self.show_toast("✓ 已刪除選取的排程提醒", level="info")

        _reload_schedules()

        footer = tk.Frame(win, bg=PALETTE["bg_app"], padx=18, pady=10)
        footer.pack(fill="x")
        ModernButton(footer, "➕ 新增排程", _add_schedule, variant="primary", padx=14, pady=6).pack(side="left")
        ModernButton(footer, "🗑️ 刪除選取排程", _delete_schedule, variant="danger", padx=14, pady=6).pack(side="left", padx=8)
        ModernButton(footer, "🔄 重新整理", _reload_schedules, variant="secondary", padx=12, pady=6).pack(side="left")
        ModernButton(footer, "關閉", win.destroy, variant="secondary", padx=14, pady=6).pack(side="right")

    def _open_engine_quick_switch_dialog(self, role: str = "master") -> None:
        """
        Quick-switch modal for Master ('master') or Guest ('guest') LLM Engine:
        - Displays detected Local Models (probed from Local API / Ollama) AND Web API option.
        - When Web API (or Local API) is selected, provides a search/filter query box
          and '檢索此 API 可用模型' button to query `/models` from the API and apply immediately.
        """
        is_master = role == "master"
        role_title = "👑 主人 (Master)" if is_master else "👤 訪客 (Guest)"
        cur_backend = self.vars["master_backend" if is_master else "guest_backend"].get().strip() or "local"
        cur_model = self.vars["master_model" if is_master else "guest_model"].get().strip() or self.vars["local_model"].get().strip()

        win = tk.Toplevel(self)
        win.title(f"⚡ 快速切換【{role_title}】推理引擎與模型")
        win.geometry("820x560")
        win.minsize(680, 460)
        win.configure(bg=PALETTE["bg_app"])
        win.transient(self)

        hdr = tk.Frame(win, bg=PALETTE["bg_app"], padx=18, pady=12)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text=f"⚡ 快速切換【{role_title}】推理引擎與模型",
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 13, "bold"),
        ).pack(side="left")
        tk.Label(
            hdr,
            text=f"目前使用：{cur_backend.upper()}  |  {cur_model}",
            bg=PALETTE["bg_app"],
            fg="#FBBF24" if is_master else PALETTE["cyan"],
            font=("Consolas", 10, "bold"),
        ).pack(side="right")

        body = tk.Frame(win, bg=PALETTE["bg_card"], highlightthickness=1, highlightbackground=PALETTE["border"], padx=16, pady=12)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        # 1. Backend Mode Selector (Local Detected Models vs Web API Option)
        mode_var = tk.StringVar(value=cur_backend if cur_backend in ("local", "web") else "local")
        selected_model_var = tk.StringVar(value=cur_model)
        search_query_var = tk.StringVar()

        mode_bar = tk.Frame(body, bg=PALETTE["bg_card"])
        mode_bar.pack(fill="x", pady=(0, 10))
        tk.Label(
            mode_bar,
            text="1️⃣ 選擇推理通道：",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 10, "bold"),
        ).pack(side="left", padx=(0, 10))

        btn_tab_local = tk.Label(
            mode_bar,
            text="🖥️ 目前偵測到的本地模型 (Local LLM)",
            bg=PALETTE["accent"],
            fg="#FFFFFF",
            font=("Microsoft JhengHei UI", 9, "bold"),
            padx=14,
            pady=6,
            cursor="hand2",
        )
        btn_tab_local.pack(side="left", padx=(0, 6))

        btn_tab_web = tk.Label(
            mode_bar,
            text="🌐 Web API 雲端模型 (檢索 API 可用模型)",
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
            padx=14,
            pady=6,
            cursor="hand2",
        )
        btn_tab_web.pack(side="left")

        # 2. API Query / Search Bar (Shown prominently for Web API model retrieval & filtering)
        search_bar = tk.Frame(body, bg=PALETTE["bg_input"], highlightthickness=1, highlightbackground=PALETTE["border"], padx=10, pady=8)
        search_bar.pack(fill="x", pady=(0, 8))

        lbl_search_title = tk.Label(
            search_bar,
            text="🔍 查詢 / 過濾可用模型：",
            bg=PALETTE["bg_input"],
            fg=PALETTE["cyan"],
            font=("Microsoft JhengHei UI", 9, "bold"),
        )
        lbl_search_title.pack(side="left")

        ent_search = tk.Entry(
            search_bar,
            textvariable=search_query_var,
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_primary"],
            insertbackground="#FFFFFF",
            relief="flat",
            font=("Consolas", 10),
        )
        ent_search.pack(side="left", fill="x", expand=True, padx=8, ipady=4)

        lbl_probe_status = tk.Label(
            body,
            text="⏳ 正在偵測可用模型...",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9),
            anchor="w",
        )
        lbl_probe_status.pack(fill="x", pady=(0, 4))

        # 3. Models Listbox
        models_lb = tk.Listbox(
            body,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            selectbackground=PALETTE["accent"],
            selectforeground="#FFFFFF",
            font=("Consolas", 10),
            relief="flat",
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            height=11,
            exportselection=False,
        )
        models_lb.pack(fill="both", expand=True, pady=(0, 8))

        # 4. Selected Model Direct Input / Confirmation Row
        sel_row = tk.Frame(body, bg=PALETTE["bg_card"])
        sel_row.pack(fill="x")
        tk.Label(
            sel_row,
            text="🎯 即將套用的模型名稱：",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
        ).pack(side="left")
        tk.Entry(
            sel_row,
            textvariable=selected_model_var,
            bg=PALETTE["bg_input"],
            fg="#34D399",
            insertbackground="#FFFFFF",
            relief="flat",
            font=("Consolas", 10, "bold"),
        ).pack(side="left", fill="x", expand=True, padx=(8, 0), ipady=4)

        cached_local_models: List[str] = []
        cached_web_models: List[str] = []
        displayed_models: List[str] = []

        def _render_filtered_list() -> None:
            nonlocal displayed_models
            m_type = mode_var.get()
            source_list = cached_web_models if m_type == "web" else cached_local_models
            kw = search_query_var.get().strip().lower()
            filtered = [m for m in source_list if not kw or kw in m.lower()]
            displayed_models = filtered
            models_lb.delete(0, "end")
            prefix_icon = "🌐 [Web API]" if m_type == "web" else "🖥️ [Local]"
            for m in filtered:
                models_lb.insert("end", f"{prefix_icon}  {m}")
            if not filtered and source_list:
                models_lb.insert("end", f"（無符合關鍵字「{kw}」的模型，共 {len(source_list)} 個可用模型）")

        def _on_lb_select(_e: Any) -> None:
            sel = models_lb.curselection()
            if not sel or sel[0] >= len(displayed_models):
                return
            selected_model_var.set(displayed_models[int(sel[0])])

        models_lb.bind("<<ListboxSelect>>", _on_lb_select)
        search_query_var.trace_add("write", lambda *_: _render_filtered_list())

        def _probe_local_models() -> None:
            lbl_probe_status.configure(text="⏳ 正在向本地 AI 伺服端 (Ollama / LM Studio) 偵測可用模型...", fg=PALETTE["warning"])

            def _worker() -> None:
                found: List[str] = []
                base = self.vars["local_base_url"].get().strip().rstrip("/")
                urls_to_try = []
                if base:
                    urls_to_try.append((base + "/models", "openai"))
                urls_to_try.extend([
                    ("http://127.0.0.1:11434/v1/models", "openai"),
                    ("http://127.0.0.1:11434/api/tags", "ollama"),
                    ("http://127.0.0.1:1234/v1/models", "openai"),
                ])
                for url, fmt in urls_to_try:
                    try:
                        req = urllib.request.Request(url, headers={"User-Agent": "Diana-Control-Center/2.1"})
                        with urllib.request.urlopen(req, timeout=2.5) as resp:
                            data = json.loads(resp.read().decode("utf-8", errors="replace"))
                        if fmt == "openai" and isinstance(data.get("data"), list):
                            for item in data["data"]:
                                mid = str(item.get("id", "")).strip()
                                if mid and mid not in found:
                                    found.append(mid)
                        elif fmt == "ollama" and isinstance(data.get("models"), list):
                            for item in data["models"]:
                                mid = str(item.get("name", "")).strip()
                                if mid and mid not in found:
                                    found.append(mid)
                    except Exception:
                        pass

                cfg_loc = self.vars["local_model"].get().strip()
                if cfg_loc and cfg_loc not in found:
                    found.insert(0, cfg_loc)

                def _update_ui() -> None:
                    if not win.winfo_exists():
                        return
                    cached_local_models.clear()
                    cached_local_models.extend(found)
                    if mode_var.get() == "local":
                        if found:
                            lbl_probe_status.configure(
                                text=f"● 本地伺服端偵測完成：共找到 {len(found)} 個本地模型（可於上方查詢框輸入關鍵字篩選）",
                                fg=PALETTE["success"],
                            )
                        else:
                            lbl_probe_status.configure(
                                text="⚠ 未偵測到執行中的本地 AI 伺服端 (Ollama / LM Studio)，可直接在下方輸入模型名稱",
                                fg=PALETTE["warning"],
                            )
                        _render_filtered_list()

                self.after(0, _update_ui)

            threading.Thread(target=_worker, daemon=True).start()

        def _probe_web_models() -> None:
            lbl_probe_status.configure(text="⏳ 正在向 Web API 檢索此 API 的可用模型列表...", fg=PALETTE["warning"])

            def _worker() -> None:
                found: List[str] = []
                err_msg = ""
                base = self.vars["web_base_url"].get().strip().rstrip("/")
                key = self.vars["chat_key"].get().strip() or ConfigRepository.read_first_line(CHAT_KEY)
                if not base:
                    err_msg = "尚未設定 Web Base URL"
                else:
                    # 1. Try standard OpenAI-compatible /models with Bearer token
                    try:
                        req = urllib.request.Request(
                            base + "/models",
                            headers={
                                "Authorization": f"Bearer {key}",
                                "User-Agent": "Diana-Control-Center/2.1",
                            },
                        )
                        with urllib.request.urlopen(req, timeout=6.0) as resp:
                            data = json.loads(resp.read().decode("utf-8", errors="replace"))
                        if isinstance(data.get("data"), list):
                            for item in data["data"]:
                                mid = str(item.get("id", "")).strip()
                                if mid.startswith("models/"):
                                    mid = mid[len("models/"):]
                                if mid and mid not in found:
                                    found.append(mid)
                    except Exception as e:
                        err_msg = str(e)

                    # 2. If Google Generative Language endpoint, also try native /v1beta/models?key=
                    if not found and "generativelanguage.googleapis.com" in base and key:
                        try:
                            g_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
                            req = urllib.request.Request(g_url, headers={"User-Agent": "Diana-Control-Center/2.1"})
                            with urllib.request.urlopen(req, timeout=6.0) as resp:
                                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                            if isinstance(data.get("models"), list):
                                for item in data["models"]:
                                    mid = str(item.get("name", "")).strip()
                                    if mid.startswith("models/"):
                                        mid = mid[len("models/"):]
                                    if mid and mid not in found:
                                        found.append(mid)
                                err_msg = ""
                        except Exception as e2:
                            err_msg = str(e2)

                cfg_web = self.vars["web_model"].get().strip()
                if cfg_web and cfg_web not in found:
                    found.insert(0, cfg_web)

                def _update_ui() -> None:
                    if not win.winfo_exists():
                        return
                    cached_web_models.clear()
                    cached_web_models.extend(found)
                    if mode_var.get() == "web":
                        if len(found) > 1 or (found and not err_msg):
                            lbl_probe_status.configure(
                                text=f"● Web API 檢索成功：共取得 {len(found)} 個可用模型！請在上方查詢框輸入關鍵字過濾並點選模型",
                                fg=PALETTE["success"],
                            )
                        else:
                            lbl_probe_status.configure(
                                text=f"⚠ Web API 模型檢索提示：{err_msg or '請確認 Chat API Key 是否已填寫'}（仍可直接選取預設或手動輸入）",
                                fg=PALETTE["warning"],
                            )
                        _render_filtered_list()

                self.after(0, _update_ui)

            threading.Thread(target=_worker, daemon=True).start()

        btn_fetch_api = ModernButton(
            search_bar,
            "🔎 檢索此 API 可用模型",
            lambda: _probe_web_models() if mode_var.get() == "web" else _probe_local_models(),
            variant="primary",
            padx=12,
            pady=4,
        )
        btn_fetch_api.pack(side="right")

        def _switch_mode(target_m: str) -> None:
            mode_var.set(target_m)
            if target_m == "local":
                btn_tab_local.configure(bg=PALETTE["accent"], fg="#FFFFFF")
                btn_tab_web.configure(bg=PALETTE["bg_input"], fg=PALETTE["text_secondary"])
                lbl_search_title.configure(text="🔍 過濾本地可用模型：")
                if not cached_local_models:
                    _probe_local_models()
                else:
                    lbl_probe_status.configure(
                        text=f"● 目前顯示本地偵測到的模型（共 {len(cached_local_models)} 個）",
                        fg=PALETTE["success"],
                    )
                    _render_filtered_list()
            else:
                btn_tab_web.configure(bg=PALETTE["accent"], fg="#FFFFFF")
                btn_tab_local.configure(bg=PALETTE["bg_input"], fg=PALETTE["text_secondary"])
                lbl_search_title.configure(text="🔍 查詢 / 檢索此 Web API 的可用模型：")
                ent_search.focus_set()
                if len(cached_web_models) <= 1:
                    _probe_web_models()
                else:
                    lbl_probe_status.configure(
                        text=f"● 目前顯示 Web API 可用模型（共 {len(cached_web_models)} 個，可輸入關鍵字即時檢索）",
                        fg=PALETTE["success"],
                    )
                    _render_filtered_list()

        btn_tab_local.bind("<Button-1>", lambda _e: _switch_mode("local"))
        btn_tab_web.bind("<Button-1>", lambda _e: _switch_mode("web"))

        def _apply_engine_choice() -> None:
            chosen_b = mode_var.get()
            chosen_m = selected_model_var.get().strip()
            if not chosen_m:
                messagebox.showwarning("請選擇或輸入模型", "請從列表中點選一個模型，或在下方輸入模型名稱。", parent=win)
                return

            if is_master:
                self.vars["master_backend"].set(chosen_b)
                self.vars["master_model"].set(chosen_m)
                self.cfg["master_backend"] = chosen_b
                self.cfg["master_model"] = chosen_m
                if chosen_b == "web":
                    self.vars["web_model"].set(chosen_m)
                    self.cfg["web_model"] = chosen_m
            else:
                self.vars["guest_backend"].set(chosen_b)
                self.vars["guest_model"].set(chosen_m)
                self.cfg["guest_backend"] = chosen_b
                self.cfg["guest_model"] = chosen_m
                if chosen_b == "local":
                    self.vars["local_model"].set(chosen_m)
                    self.cfg["local_model"] = chosen_m

            ConfigRepository.write_json(GUI_CONFIG, self.cfg)
            self._refresh_dashboard_telemetry()
            self.show_toast(
                f"✓ 已將【{role_title}】推理引擎切換為：{chosen_b.upper()} ({chosen_m})",
                level="success",
            )
            win.destroy()

        models_lb.bind("<Double-Button-1>", lambda _e: _apply_engine_choice())

        # Start initial probe
        _probe_local_models()
        _switch_mode(mode_var.get())

        footer = tk.Frame(win, bg=PALETTE["bg_app"], padx=18, pady=10)
        footer.pack(fill="x")
        ModernButton(
            footer,
            "🔄 重新偵測本地模型",
            _probe_local_models,
            variant="secondary",
            padx=12,
            pady=6,
        ).pack(side="left")
        ModernButton(
            footer,
            "🌐 重新檢索 Web API 模型",
            _probe_web_models,
            variant="secondary",
            padx=12,
            pady=6,
        ).pack(side="left", padx=8)
        ModernButton(
            footer,
            f"✅ 立即套用至【{role_title}】",
            _apply_engine_choice,
            variant="primary",
            padx=18,
            pady=6,
        ).pack(side="right")
        ModernButton(footer, "取消", win.destroy, variant="secondary", padx=12, pady=6).pack(side="right", padx=8)

    # -----------------------------------------------------------------------
    # Worker Trigger Actions
    # -----------------------------------------------------------------------
    def action_open_patch_pusher(self) -> None:
        if self.workers.launch_patch_pusher():
            self.show_toast("🩹 已開啟 Diana Patch Pusher 補丁推送工具", level="info")
        else:
            self.show_toast("⚠ 找不到 DianaPatchPusher.exe 或 patch_pusher.py", level="warn")

    def action_open_seed_installer(self) -> None:
        if self.workers.launch_seed_installer():
            self.show_toast("🌱 已開啟 Diana-Bot 冷安裝與種子封裝代理 (DianaSeedInstaller.exe)", level="info")
        else:
            self.show_toast("⚠ 找不到或無法編譯 DianaSeedInstaller.exe", level="warn")

    def action_pack_clean_seed(self) -> None:
        self.switch_page("dashboard")
        self.workers.pack_clean_seed_async(DEFAULT_SEED_FILE)

    def apply_ollama_preset(self) -> None:
        self.vars["local_base_url"].set("http://127.0.0.1:11434/v1")
        self.vars["local_model"].set("gemma4:e2b")
        self.show_toast("🦙 已套用 Ollama 預設端點 (11434) 與模型 (gemma4:e2b)，請點選儲存設定", level="info")

    def apply_lmstudio_preset(self) -> None:
        self.vars["local_base_url"].set("http://127.0.0.1:1234/v1")
        self.vars["local_model"].set("google/gemma-4-e2b")
        self.show_toast("🖥️ 已套用 LM Studio 預設端點 (1234) 與模型 (google/gemma-4-e2b)，請點選儲存設定", level="info")

    def action_start_bot(self) -> None:
        if self.workers.is_bot_alive():
            self.show_toast("ℹ Diana 目前已經在運行中", level="info")
            return
        if not self.save_all_settings(silent=True):
            return
        self.workers.start_bot_process()
        self.show_toast("▶ 已啟動 Diana Bot 背景行程", level="success")

    def action_stop_bot(self) -> None:
        if self.workers.stop_bot_process():
            self.show_toast("■ 已傳送停止訊號給 Diana Bot", level="warn")
        else:
            self.show_toast("ℹ Diana 目前並未在執行", level="info")

    def action_restart_bot(self) -> None:
        self.workers.stop_bot_process()
        self.show_toast("↻ 正在重新啟動 Diana Bot...", level="info")
        self.after(900, self.action_start_bot)

    def action_test_local(self) -> None:
        self.btn_test_local.set_enabled(False, "測試中...")
        self.workers.test_openai_endpoint_async(
            target_id="local",
            base_url=self.vars["local_base_url"].get(),
            api_key="",
            label="本地 Local API",
        )

    def action_test_web(self) -> None:
        self.btn_test_web.set_enabled(False, "測試中...")
        self.workers.test_openai_endpoint_async(
            target_id="web",
            base_url=self.vars["web_base_url"].get(),
            api_key=self.vars["chat_key"].get(),
            label="雲端 Web API",
        )

    def action_verify_image_cfg(self) -> None:
        ep = self.vars["image_endpoint"].get().strip()
        mod = self.vars["image_model"].get().strip()
        key = self.vars["image_key"].get().strip()
        if ep and mod and key:
            self.lbl_test_img.configure(text=f"● 設定完整 (Model: {mod})", fg=PALETTE["success"])
            self.show_toast("✓ Image API 端點與獨立金鑰皆已填寫完整", level="success")
        else:
            self.lbl_test_img.configure(text="● 缺少 Endpoint、Model 或 Image API Key", fg=PALETTE["warning"])
            self.show_toast("⚠ 請確認 Image Endpoint、Model 與 Image API Key 皆已填寫", level="warn")

    def action_generate_personas(self) -> None:
        desc = self.persona_prompt_var.get().strip()
        if not desc:
            messagebox.showwarning("請輸入角色描述", "請先在「AI 角色生成」輸入框填寫你想建立的角色特色或背景描述。")
            return
        if self.lifecycle.is_generating_persona_flag:
            self.show_toast("⏳ 正在生成候選角色中，請稍候...", level="info")
            return
        use_web = self.persona_gen_backend_var.get() == "web"
        self.workers.generate_personas_async(
            description=desc,
            use_web_api=use_web,
            local_base_url=self.vars["local_base_url"].get(),
            local_model=self.vars["local_model"].get(),
            web_base_url=self.vars["web_base_url"].get(),
            web_model=self.vars["web_model"].get(),
            chat_key=self.vars["chat_key"].get(),
        )

    def action_install_deps(self) -> None:
        if self.lifecycle.is_installing_deps_flag:
            return
        self.switch_page("dashboard")
        self.workers.install_dependencies_async()

    def _restart_with_python_312(self, executable: str) -> None:
        if not self.save_all_settings(silent=True):
            return

        python_executable = Path(executable)
        pythonw_executable = python_executable.with_name("pythonw.exe")
        runner = pythonw_executable if pythonw_executable.is_file() else python_executable
        try:
            subprocess.Popen([str(runner), str(ROOT / "gui.py"), "--install-deps"], cwd=ROOT)
        except OSError as e:
            messagebox.showerror("重新啟動失敗", f"無法使用 Python 3.12 重新啟動中控台：\n{e}")
            return
        self.destroy()

    def action_run_evolution_now(self) -> None:
        if self.lifecycle.is_running_evolution_flag:
            return
        self.save_all_settings(silent=True)
        self.switch_page("dashboard")
        self.workers.run_evolution_now_async()

    # -----------------------------------------------------------------------
    # Candidate Persona Selection Dialog
    # -----------------------------------------------------------------------
    def _open_persona_candidates_dialog(self, candidates: List[Dict[str, Any]]) -> None:
        win = tk.Toplevel(self)
        win.title("✨ 選擇 AI 生成的 Persona 候選方案")
        win.geometry("920x620")
        win.minsize(720, 480)
        win.resizable(True, True)
        win.configure(bg=PALETTE["bg_app"])

        hdr = tk.Frame(win, bg=PALETTE["bg_app"], padx=20, pady=14)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text="✨ LLM 已為您生成三個角色方案，請預覽並選擇要套用的方案：",
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 13, "bold"),
        ).pack(side="left")

        tab_bar = tk.Frame(win, bg=PALETTE["bg_card"], padx=8, pady=6)
        tab_bar.pack(fill="x", padx=20)

        content_area = tk.Frame(win, bg=PALETTE["bg_card"], padx=14, pady=14)
        content_area.pack(fill="both", expand=True, padx=20, pady=10)

        selected_idx = tk.IntVar(value=0)
        preview_box = scrolledtext.ScrolledText(
            content_area,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            font=("Consolas", 10),
            relief="flat",
            padx=12,
            pady=12,
        )
        preview_box.pack(fill="both", expand=True)

        tab_btns: List[tk.Label] = []

        def _select_candidate(idx: int) -> None:
            selected_idx.set(idx)
            for i, b in enumerate(tab_btns):
                b.configure(
                    bg=PALETTE["accent"] if i == idx else PALETTE["bg_card"],
                    fg="#FFFFFF" if i == idx else PALETTE["text_secondary"],
                )
            preview_box.configure(state="normal")
            preview_box.delete("1.0", "end")
            preview_box.insert("1.0", json.dumps(candidates[idx], ensure_ascii=False, indent=2))
            preview_box.configure(state="disabled")

        for i, cand in enumerate(candidates[:3]):
            lbl_name = cand.get("name", f"候選 {i + 1}") if isinstance(cand, dict) else f"候選 {i + 1}"
            b = tk.Label(
                tab_bar,
                text=f"方案 {chr(65 + i)} · {lbl_name}",
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_secondary"],
                font=("Microsoft JhengHei UI", 10, "bold"),
                padx=16,
                pady=8,
                cursor="hand2",
            )
            b.pack(side="left", padx=4)
            b.bind("<Button-1>", lambda _e, ix=i: _select_candidate(ix))
            tab_btns.append(b)

        _select_candidate(0)

        footer = tk.Frame(win, bg=PALETTE["bg_app"], padx=20, pady=12)
        footer.pack(fill="x")

        def _apply() -> None:
            chosen = candidates[selected_idx.get()]
            if isinstance(chosen, dict):
                self._set_persona_object(chosen)
                self.show_toast(f"✓ 已套用「{chosen.get('name', '新角色')}」至編輯器，確認無誤後請點選儲存", level="success")
            win.destroy()

        ModernButton(footer, "✓ 套用此方案至編輯器", _apply, variant="primary", padx=20, pady=8).pack(side="right")
        ModernButton(footer, "取消", win.destroy, variant="secondary", padx=14, pady=8).pack(side="right", padx=8)

    # -----------------------------------------------------------------------
    # Log Console Helpers
    # -----------------------------------------------------------------------
    @staticmethod
    def _classify_log_line(line: str, explicit_tag: str = "") -> str:
        if explicit_tag:
            return explicit_tag
        if "❌" in line or "Error" in line or "Traceback" in line or "失敗" in line:
            return "error"
        if "⚠️" in line or "Warning" in line:
            return "warn"
        if "✅" in line or "成功" in line:
            return "success"
        if "📊" in line:
            return "metric"
        if "===" in line:
            return "system"
        return ""

    def _append_log_line(self, text: str, explicit_tag: str = "") -> None:
        tag = self._classify_log_line(text, explicit_tag)
        self._raw_log_buffer.append((text, tag))
        if len(self._raw_log_buffer) > 2500:
            self._raw_log_buffer = self._raw_log_buffer[-2500:]

        flt = self.log_filter_var.get().strip().lower()
        if not flt or flt in text.lower():
            if tag:
                self.log_text.insert("end", text, tag)
            else:
                self.log_text.insert("end", text)
            if self.auto_scroll_var.get():
                self.log_text.see("end")

    def _rerender_filtered_logs(self) -> None:
        flt = self.log_filter_var.get().strip().lower()
        self.log_text.delete("1.0", "end")
        for text, tag in self._raw_log_buffer:
            if not flt or flt in text.lower():
                if tag:
                    self.log_text.insert("end", text, tag)
                else:
                    self.log_text.insert("end", text)
        if self.auto_scroll_var.get():
            self.log_text.see("end")

    def clear_log_console(self) -> None:
        self._raw_log_buffer.clear()
        self.log_text.delete("1.0", "end")
        self.show_toast("✓ 已清除執行日誌畫面", level="info")

    def copy_log_console(self) -> None:
        content = self.log_text.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(content)
        self.show_toast("✓ 已將執行日誌複製至剪貼簿", level="success")

    # -----------------------------------------------------------------------
    # Periodic Event & Signal Dispatcher Loop
    # -----------------------------------------------------------------------
    def _process_signals(self) -> None:
        try:
            while True:
                sig = self.signal_queue.get_nowait()
                stype = sig.signal_type
                p = sig.payload

                if stype == "LOG_APPEND":
                    self._append_log_line(p.get("text", ""), p.get("tag", ""))

                elif stype == "API_TEST_STATUS":
                    tid = p.get("target_id")
                    status = p.get("status")
                    msg = p.get("message", "")
                    lbl_widget = self.lbl_test_local if tid == "local" else self.lbl_test_web
                    btn_widget = self.btn_test_local if tid == "local" else self.btn_test_web
                    btn_text = "⚡ 測試本地 API 連線" if tid == "local" else "⚡ 測試 Web API 連線"

                    if status == "testing":
                        lbl_widget.configure(text=f"⏳ {msg}", fg=PALETTE["warning"])
                    elif status == "ok":
                        lbl_widget.configure(text=f"● {msg}", fg=PALETTE["success"])
                        btn_widget.set_enabled(True, btn_text)
                        self.show_toast(f"✓ {p.get('label', 'API')} {msg}", level="success")
                    else:
                        lbl_widget.configure(text=f"● {msg}", fg=PALETTE["danger"])
                        btn_widget.set_enabled(True, btn_text)
                        self.show_toast(f"✗ {p.get('label', 'API')} 連線失敗", level="error")

                elif stype == "PERSONA_GEN_STARTED":
                    self.btn_gen_persona.set_enabled(False, "⏳ AI 思考生成中...")
                    self.show_toast("✨ 正在呼叫 LLM 產生三個候選角色方案...", level="info")

                elif stype == "PERSONA_GEN_SUCCESS":
                    self.btn_gen_persona.set_enabled(True, "✨ 生成三個候選方案")
                    self._open_persona_candidates_dialog(p.get("candidates", []))

                elif stype == "PERSONA_GEN_ERROR":
                    self.btn_gen_persona.set_enabled(True, "✨ 生成三個候選方案")
                    messagebox.showerror("LLM 生成角色失敗", str(p.get("error", "未知錯誤")))

                elif stype == "DEPS_INSTALL_STARTED":
                    self.btn_install_deps.set_enabled(False, "⏳ 正在安裝依賴套件...")
                    self.show_toast("📦 正在安裝依賴套件...", level="info")

                elif stype == "DEPS_INSTALL_FINISHED":
                    self.btn_install_deps.set_enabled(True, "📦 安裝依賴")
                    self.refresh_file_diagnostics()
                    rc = p.get("returncode", -1)
                    if rc == 0:
                        self.show_toast("✓ 依賴套件安裝完成", level="success")
                    else:
                        self.show_toast(f"⚠ 依賴安裝失敗 (代碼 {rc})，請查看日誌", level="warn")
                        python312_path = p.get("python312_path")
                        if python312_path:
                            if messagebox.askyesno(
                                "建議改用 Python 3.12",
                                "目前 Python 環境無法完成依賴安裝。\n"
                                "已找到 Python 3.12，是否現在重新啟動 Diana 中控台，並自動在 Python 3.12 重試安裝？",
                            ):
                                self._restart_with_python_312(python312_path)
                        elif sys.version_info[:2] == (3, 12):
                            messagebox.showwarning(
                                "依賴安裝未完成",
                                "目前已經使用 Python 3.12，但依賴仍未安裝成功。\n"
                                "請查看執行日誌確認 pip 錯誤原因。",
                            )
                        else:
                            messagebox.showwarning(
                                "建議安裝 Python 3.12",
                                "目前 Python 環境無法完成依賴安裝，且找不到可用的 Python 3.12。\n"
                                "建議先安裝 Python 3.12，然後重新啟動 Diana 中控台再安裝依賴。\n"
                                "詳細錯誤請查看執行日誌。",
                            )

                elif stype == "EVOLUTION_RUN_STARTED":
                    self.btn_run_evolution.set_enabled(False, "⏳ 演化進行中...")
                    self.show_toast("🧬 已啟動 Diana Evolution 單次演化程序...", level="info")

                elif stype == "EVOLUTION_RUN_FINISHED":
                    self.btn_run_evolution.set_enabled(True, "🧬 立即執行一次演化 (diana_evolution.py --run-now)")
                    self.load_current_persona_file(silent=True)
                    self._refresh_dashboard_telemetry()
                    rc = p.get("returncode", -1)
                    if rc == 0:
                        self.show_toast("✓ 演化程序執行完畢，已同步最新狀態", level="success")
                    else:
                        self.show_toast(f"⚠ 演化程序結束 (代碼 {rc})，請查看日誌", level="warn")

                elif stype == "SEED_PACK_STARTED":
                    if hasattr(self, "btn_pack_seed"):
                        self.btn_pack_seed.set_enabled(False, "⏳ 正在封裝無記憶 .seed...")
                    self.show_toast("🌱 正在封裝無記憶 .seed 系統包...", level="info")

                elif stype == "SEED_PACK_FINISHED":
                    if hasattr(self, "btn_pack_seed"):
                        self.btn_pack_seed.set_enabled(True, "🌱 立即封裝無記憶 .seed 系統包 (Diana-Bot.seed)")
                    self.refresh_file_diagnostics()
                    rc = p.get("returncode", -1)
                    seed_out = p.get("path", "Diana-Bot.seed")
                    if rc == 0:
                        self.show_toast(f"✓ 無記憶系統包已封裝完成：{Path(seed_out).name}", level="success")
                    else:
                        self.show_toast(f"⚠ 封裝 .seed 失敗 (代碼 {rc})，請查看日誌", level="warn")

        except queue.Empty:
            pass

    def _refresh_dashboard_telemetry(self) -> None:
        snap = ConfigRepository.collect_telemetry_snapshot()
        running = self.workers.is_bot_alive()
        rt = snap["runtime"]
        stage = rt.get("stage", "idle") if running else ""
        detail = rt.get("detail", "正在啟動連線…") if running else "尚未啟動 Diana Bot"
        backend = rt.get("backend", "") if running else ""
        pid = rt.get("pid", "—") if running else "—"
        updated_at = rt.get("updated_at", "—")

        # Update Sidebar & Brand Header
        self.ver_badge.configure(text=f"v{snap['bot_version']}")
        self.brand_dot.configure(fg=PALETTE["success"] if running else PALETTE["text_muted"])
        self.sidebar_status_lbl.configure(
            text="狀態：● ONLINE (運行中)" if running else "狀態：○ OFFLINE (待機)",
            fg=PALETTE["success"] if running else PALETTE["text_secondary"],
        )
        self.sidebar_sub_lbl.configure(text=f"PID: {pid}  |  Backend: {(backend or '—').upper()}")

        # Update KPI Card 1: Status
        self.kpi_cards["status"]["val"].configure(
            text="● ONLINE" if running else "○ OFFLINE",
            fg=PALETTE["success"] if running else PALETTE["text_secondary"],
        )
        self.kpi_cards["status"]["sub"].configure(text=detail[:36])

        # Update KPI Card 2: Persona & Quick-Switch
        self.kpi_cards["persona"]["val"].configure(text=snap["persona_name"])
        self.kpi_cards["persona"]["sub"].configure(
            text=f"v{snap['persona_version']} · 偵測到 {snap['persona_files_count']} 個 .persona"
        )

        # Update KPI Card 3: Split [關係 (訪客)] & [排程 (提醒)]
        self.kpi_rel_val.configure(text=f"{snap['guest_count']} 位訪客")
        self.kpi_rel_sub.configure(text=f"認知筆記 {snap['guest_note_count']} 則 · 點擊查看")
        self.kpi_sched_val.configure(text=f"{snap['reminder_count']} 筆排程")
        self.kpi_sched_sub.configure(text=snap["next_reminder_hint"])

        # Update KPI Card 4: Split [主人推理引擎] & [訪客推理引擎]
        master_b = (self.vars["master_backend"].get().strip() or "local").lower()
        master_m = (self.vars["master_model"].get().strip() or self.vars["local_model"].get().strip()).split("/")[-1][:18]
        guest_b = (self.vars["guest_backend"].get().strip() or "local").lower()
        guest_m = (self.vars["guest_model"].get().strip() or self.vars["local_model"].get().strip()).split("/")[-1][:18]

        self.kpi_eng_master_val.configure(text="🌐 Web API" if master_b == "web" else "🖥️ Local")
        self.kpi_eng_master_sub.configure(text=f"{master_m or '未設定'} · 點擊切換")
        self.kpi_eng_guest_val.configure(text="🌐 Web API" if guest_b == "web" else "🖥️ Local")
        self.kpi_eng_guest_sub.configure(text=f"{guest_m or '未設定'} · 點擊切換")

        # Update Pipeline Stepper
        self.pipeline_canvas.set_state(stage=stage, is_online=running, advance_pulse=True)
        backend_tag = f"  [{backend.upper()}]" if backend else ""
        self.stage_detail_lbl.configure(
            text=f"目前階段：{detail}{backend_tag}",
            fg=PALETTE["danger"] if stage == "error" else (PALETTE["text_primary"] if running else PALETTE["text_secondary"]),
        )
        self.stage_meta_lbl.configure(text=f"最後狀態更新：{updated_at}")

    def _tick_loop(self) -> None:
        self._process_signals()
        self._refresh_dashboard_telemetry()
        self.after(350, self._tick_loop)

    # -----------------------------------------------------------------------
    # Utility & Window Lifecycle
    # -----------------------------------------------------------------------
    def open_project_folder(self) -> None:
        if sys.platform.startswith("win"):
            os.startfile(ROOT)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(ROOT)])
        else:
            subprocess.Popen(["xdg-open", str(ROOT)])

    def _show_first_run_welcome(self) -> None:
        if messagebox.askyesno(
            "歡迎使用 Diana Bot Control Center",
            "偵測到這是第一次啟動中控台！\n\n"
            "您可以在此完成 Local / Web / Image API、Discord Token、角色設定與套件安裝。\n"
            "是否現在前往「連線與金鑰」頁面進行初始設定？",
        ):
            self.switch_page("connections")

    def _on_close_window(self) -> None:
        if self.workers.is_bot_alive():
            if messagebox.askyesno("關閉 Diana Control Center", "Diana Bot 目前仍在背景運行中。\n請問要一併停止 Diana Bot 嗎？"):
                self.workers.stop_bot_process()
        self.destroy()


# Backwards-compatible alias
App = DianaControlCenterApp

if __name__ == "__main__":
    app = DianaControlCenterApp()
    if "--install-deps" in sys.argv[1:]:
        app.after(500, app.action_install_deps)
    app.mainloop()
