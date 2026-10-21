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
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
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

TOKEN = CONFIG / "discord_token.txt"
CHAT_KEY = CONFIG / "google_api_key_chat.txt"
EVOLUTION_KEY = CONFIG / "google_api_key_evolution.txt"
IMAGE_KEY = CONFIG / "google_api_key_image.txt"

STATUS_FILE = DATA / "runtime_status.json"
VERSION_FILE = DATA / "version.json"
BOT_STATE_FILE = DATA / "bot_state.json"
FAVORABILITY_FILE = DATA / "favorability.json"
REMINDERS_FILE = DATA / "reminders.json"

DEFAULT_CFG: Dict[str, Any] = {
    "local_base_url": "http://127.0.0.1:1234/v1",
    "local_model": "google/gemma-4-e2b",
    "web_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "web_model": "gemini-3.5-flash-lite",
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
    workspace_width: int = 985
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

        sidebar_w = 68 if collapsed else 232
        ws_w = max(win_w - sidebar_w, 360)

        kpi_cols = 2 if ws_w < cls.COMPACT_WORKSPACE_W else 4
        dual_forms = ws_w >= cls.COMPACT_WORKSPACE_W
        compact_tb = ws_w < 860

        changed = (
            bp != current.breakpoint
            or collapsed != current.sidebar_collapsed
            or kpi_cols != current.kpi_columns
            or dual_forms != current.dual_col_forms
            or compact_tb != current.compact_toolbar
            or abs(ws_w - current.workspace_width) >= 24
        )

        new_state = ViewportLayoutState(
            window_width=win_w,
            window_height=win_h,
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

    @staticmethod
    def blank_persona_template() -> Dict[str, Any]:
        return {
            "name": "",
            "age_style": "",
            "background": "",
            "relationship": "",
            "guest_attitude": "",
            "personality_traits": [""],
            "speech_patterns": [""],
            "tone_rules": [""],
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

        return {
            "bot_version": version_data.get("bot", "—"),
            "persona_version": version_data.get("character_persona", "—"),
            "persona_name": persona_data.get("name", "未設定") if isinstance(persona_data, dict) else "未設定",
            "persona_age_style": persona_data.get("age_style", "") if isinstance(persona_data, dict) else "",
            "user_count": user_count,
            "max_favor": max_favor,
            "reminder_count": reminder_count,
            "action_img_count": action_img_count,
            "last_message_time": bot_state.get("last_message_time", "無紀錄"),
            "runtime": runtime_status,
        }


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
        if not PATCH_PUSHER_SCRIPT.exists():
            return False
        exe_dir = Path(sys.executable).resolve().parent
        pythonw = exe_dir / "pythonw.exe"
        runner = str(pythonw) if (sys.platform.startswith("win") and pythonw.exists()) else sys.executable
        subprocess.Popen([runner, str(PATCH_PUSHER_SCRIPT)], cwd=str(ROOT))
        return True

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
                    text=f"\n[{datetime.now().strftime('%H:%M:%S')}] === 📦 開始安裝 requirements.txt 依賴套件 ===\n",
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
                self.emit("DEPS_INSTALL_FINISHED", returncode=proc.returncode)
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

            # 1. Reflow Collapsible Sidebar Rail (68px icon mode vs 232px full mode)
            target_sb_w = 68 if vp.sidebar_collapsed else 232
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
                    if not widgets["desc"].winfo_ismapped():
                        widgets["desc"].pack(fill="x", pady=(2, 0))
                    widgets["inner"].configure(padx=12, pady=9)

            # 2. Reflow Dashboard 4 KPI Cards (1x4 wide vs 2x2 compact)
            cols = vp.kpi_columns
            for c in range(4):
                self.kpi_row.columnconfigure(c, weight=1 if c < cols else 0, uniform="kpi" if c < cols else "")

            card_wrap_px = max((vp.workspace_width - 64) // max(cols, 1) - 28, 130)
            for idx, key in enumerate(("status", "persona", "social", "engine")):
                card_info = self.kpi_cards[key]
                frame = card_info["frame"]
                r = idx // cols
                c = idx % cols
                pad_l = 0 if c == 0 else 5
                pad_r = 0 if c == (cols - 1) else 5
                pad_b = 6 if (cols == 2 and r == 0) else 0
                frame.grid_configure(row=r, column=c, sticky="nsew", padx=(pad_l, pad_r), pady=(0, pad_b))
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
        # Left Collapsible Sidebar Navigation Rail
        self.sidebar = tk.Frame(
            self,
            bg=PALETTE["bg_sidebar"],
            width=232,
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

        if PATCH_PUSHER_SCRIPT.exists():
            ModernButton(
                self.topbar,
                "🩹 開啟補丁推送工具",
                self.action_open_patch_pusher,
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
            for pid in ("dashboard", "connections", "persona", "system")
        }

        self._build_dashboard_page()
        self._build_connections_page()
        self._build_persona_page()
        self._build_system_page()
        self.switch_page("dashboard")

    def switch_page(self, page_id: str) -> None:
        self.lifecycle.active_page = page_id
        titles = {
            "dashboard": "即時儀表板 (Runtime Dashboard)",
            "connections": "連線與獨立憑證設定 (API & Credentials)",
            "persona": "角色設定工坊 (Persona Studio)",
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
        kpi_defs = [
            ("status", "執行狀態 (RUNTIME)", "OFFLINE", "尚未啟動 bot.py"),
            ("persona", "目前角色 (PERSONA)", "黛安娜", "Bot v— · Persona v—"),
            ("social", "關係與排程 (MEMORY)", "0 位互動對象", "最高好感 0 · 提醒 0 筆"),
            ("engine", "推理引擎 (LLM ENGINE)", "Local + Web", "Temp 0.75 · Think 256"),
        ]
        for idx, (key, title, val, sub) in enumerate(kpi_defs):
            c = tk.Frame(
                self.kpi_row,
                bg=PALETTE["bg_card"],
                highlightthickness=1,
                highlightbackground=PALETTE["border"],
                padx=14,
                pady=10,
            )
            c.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 5, 0 if idx == 3 else 5))
            tk.Label(
                c,
                text=title,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_muted"],
                font=("Segoe UI", 8, "bold"),
                anchor="w",
            ).pack(fill="x")
            lbl_val = tk.Label(
                c,
                text=val,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_primary"],
                font=("Microsoft JhengHei UI", 13, "bold"),
                anchor="w",
            )
            lbl_val.pack(fill="x", pady=(3, 1))
            lbl_sub = tk.Label(
                c,
                text=sub,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_secondary"],
                font=("Microsoft JhengHei UI", 9),
                anchor="w",
                justify="left",
            )
            lbl_sub.pack(fill="x")
            self.kpi_cards[key] = {"frame": c, "val": lbl_val, "sub": lbl_sub}

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
            subtitle="適用 LM Studio / Ollama / vLLM 等 OpenAI 相容端點",
        )
        c_local.pack(fill="x")
        self._create_labeled_entry(c_local, "Base URL", self.vars["local_base_url"], "例: http://127.0.0.1:1234/v1")
        self._create_labeled_entry(c_local, "Model Name", self.vars["local_model"], "例: google/gemma-4-e2b")
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

        ModernButton(btn_row, "💾 儲存 Persona 設定", self.save_persona, variant="primary", padx=15, pady=6).pack(side="right")

        # Mode Switcher (Visual Form vs Raw JSON)
        self.persona_mode = "visual"
        self.btn_mode_json = ModernButton(btn_row, "{ } JSON 原始碼模式", lambda: self.switch_persona_editor_mode("json"), variant="ghost", padx=11, pady=6)
        self.btn_mode_json.pack(side="right", padx=5)
        self.btn_mode_visual = ModernButton(btn_row, "📝 視覺化表單模式", lambda: self.switch_persona_editor_mode("visual"), variant="secondary", padx=11, pady=6)
        self.btn_mode_visual.pack(side="right", padx=5)

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

        self.p_bg_text = _make_text_box(self.p_left_col, "角色背景設定 (background)", 4)
        self.p_rel_text = _make_text_box(self.p_left_col, "與主人的關係 (relationship)", 3)
        self.p_guest_text = _make_text_box(self.p_left_col, "對待訪客態度 (guest_attitude)", 3)
        self.p_favor_text = _make_text_box(self.p_left_col, "好感度三階段反應 (低 / 中 / 高，每行一段)", 4)

        self.p_traits_text = _make_text_box(self.p_right_col, "性格特質 (personality_traits — 每行一條)", 5)
        self.p_speech_text = _make_text_box(self.p_right_col, "說話習慣 (speech_patterns — 每行一條)", 5)
        self.p_tone_text = _make_text_box(self.p_right_col, "語氣與行為規範 (tone_rules — 每行一條)", 5)

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

        fav = obj.get("favorability_reactions", {})
        if not isinstance(fav, dict):
            fav = {}
        fav_lines = [
            f"[低] {fav.get('low', '')}",
            f"[中] {fav.get('medium', '')}",
            f"[高] {fav.get('high', '')}",
        ]
        _set_txt(self.p_favor_text, "\n".join(fav_lines))

        _set_txt(self.p_traits_text, "\n".join(str(x) for x in obj.get("personality_traits", []) if str(x).strip()))
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
            return items or [""]

        base_obj["personality_traits"] = _lines(self.p_traits_text)
        base_obj["speech_patterns"] = _lines(self.p_speech_text)
        base_obj["tone_rules"] = _lines(self.p_tone_text)

        fav_raw = [line.strip() for line in self.p_favor_text.get("1.0", "end").splitlines() if line.strip()]
        fav_dict = {"low": "", "medium": "", "high": ""}
        for idx, key in enumerate(("low", "medium", "high")):
            if idx < len(fav_raw):
                val = fav_raw[idx]
                for prefix in ("[低]", "[中]", "[高]"):
                    if val.startswith(prefix):
                        val = val[len(prefix):].strip()
                fav_dict[key] = val
        base_obj["favorability_reactions"] = fav_dict
        return base_obj

    def switch_persona_editor_mode(self, target_mode: str) -> None:
        if target_mode == self.persona_mode:
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
                return
            self.persona_json_frame.pack_forget()
            self.persona_scroll_host.pack(fill="both", expand=True)
        self.persona_mode = target_mode

    def _set_persona_object(self, obj: Dict[str, Any]) -> None:
        self.persona_text.delete("1.0", "end")
        self.persona_text.insert("1.0", json.dumps(obj, ensure_ascii=False, indent=2))
        self._sync_json_to_visual_form(obj)

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
            subtitle="自動驗證設定檔、獨立金鑰與參考圖資源狀態（支援視窗寬度自適應排版）",
        )
        diag_card.pack(fill="x", pady=(0, 12))

        self.diag_grid = tk.Frame(diag_card, bg=PALETTE["bg_card"])
        self.diag_grid.pack(fill="x", pady=(0, 10))
        self.diag_cells: List[tk.Frame] = []

        diag_btn_row = tk.Frame(diag_card, bg=PALETTE["bg_card"])
        diag_btn_row.pack(fill="x")

        self.btn_install_deps = ModernButton(
            diag_btn_row,
            "📦 安裝 / 更新 requirements.txt",
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

    def refresh_file_diagnostics(self) -> None:
        for child in self.diag_grid.winfo_children():
            child.destroy()
        self.diag_cells.clear()

        ref_count = len(list(REFERENCE_IMG_DIR.glob("*.*"))) if REFERENCE_IMG_DIR.exists() else 0
        checks = [
            ("核心主程式 (bot.py)", (ROOT / "bot.py").exists(), "bot.py"),
            ("演化引擎 (diana_evolution.py)", (ROOT / "diana_evolution.py").exists(), "diana_evolution.py"),
            ("角色設定檔 (character_persona.json)", PERSONA.exists(), "config/character_persona.json"),
            ("Discord Bot Token", bool(ConfigRepository.read_first_line(TOKEN)), "config/discord_token.txt"),
            ("Chat API Key (聊天專用)", bool(ConfigRepository.read_first_line(CHAT_KEY)), "config/google_api_key_chat.txt"),
            ("Evolution API Key (演化專用)", bool(ConfigRepository.read_first_line(EVOLUTION_KEY)), "config/google_api_key_evolution.txt"),
            ("Image API Key (生圖專用)", bool(ConfigRepository.read_first_line(IMAGE_KEY)), "config/google_api_key_image.txt"),
            (f"角色參考圖庫 ({ref_count} 張)", ref_count > 0, "img/reference/"),
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
            badge_txt = "✓ 已就緒" if ok else "⚠ 待設定"
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

        self.vars["discord"].set(ConfigRepository.read_first_line(TOKEN))
        self.vars["chat_key"].set(ConfigRepository.read_first_line(CHAT_KEY))
        self.vars["evolution_key"].set(ConfigRepository.read_first_line(EVOLUTION_KEY))
        self.vars["evolution_model"].set(ConfigRepository.read_second_line(EVOLUTION_KEY))
        self.vars["image_key"].set(ConfigRepository.read_first_line(IMAGE_KEY))
        self.vars["temperature"].set(str(self.cfg.get("temperature", 0.75)))
        self.vars["thinking_tokens"].set(str(self.cfg.get("thinking_tokens", 256)))

        self.load_current_persona_file(silent=True)
        self.refresh_file_diagnostics()
        self._refresh_dashboard_telemetry()

    def save_all_settings(self, silent: bool = False) -> bool:
        try:
            for k in ["local_base_url", "local_model", "web_base_url", "web_model", "image_endpoint", "image_model"]:
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

    def save_persona(self) -> None:
        try:
            if self.persona_mode == "visual":
                obj = self._collect_visual_form_to_dict()
                self.persona_text.delete("1.0", "end")
                self.persona_text.insert("1.0", json.dumps(obj, ensure_ascii=False, indent=2))
            else:
                obj = json.loads(self.persona_text.get("1.0", "end"))
                self._sync_json_to_visual_form(obj)

            ConfigRepository.write_json(PERSONA, obj)
            self._refresh_dashboard_telemetry()
            self.show_toast(f"✓ 角色「{obj.get('name', '未命名')}」已儲存至 config/character_persona.json", level="success")
        except Exception as e:
            messagebox.showerror("Persona 儲存失敗", f"JSON 格式無效或寫入失敗：\n{e}")

    # -----------------------------------------------------------------------
    # Worker Trigger Actions
    # -----------------------------------------------------------------------
    def action_open_patch_pusher(self) -> None:
        if self.workers.launch_patch_pusher():
            self.show_toast("🩹 已開啟 Diana Patch Pusher 補丁推送工具", level="info")
        else:
            self.show_toast("⚠ 找不到 patch_pusher.py", level="warn")

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
                    self.show_toast("📦 正在執行 pip install -r requirements.txt...", level="info")

                elif stype == "DEPS_INSTALL_FINISHED":
                    self.btn_install_deps.set_enabled(True, "📦 安裝 / 更新 requirements.txt")
                    rc = p.get("returncode", -1)
                    if rc == 0:
                        self.show_toast("✓ requirements.txt 依賴套件安裝完成", level="success")
                    else:
                        self.show_toast(f"⚠ 套件安裝結束 (代碼 {rc})，請查看日誌", level="warn")

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

        # Update KPI Card 2: Persona & Version
        self.kpi_cards["persona"]["val"].configure(text=snap["persona_name"])
        self.kpi_cards["persona"]["sub"].configure(
            text=f"Bot v{snap['bot_version']} · Persona v{snap['persona_version']}"
        )

        # Update KPI Card 3: Social & Memory
        self.kpi_cards["social"]["val"].configure(text=f"{snap['user_count']} 位互動對象")
        self.kpi_cards["social"]["sub"].configure(
            text=f"最高好感 {snap['max_favor']} · 提醒 {snap['reminder_count']} 筆 · 插圖 {snap['action_img_count']}"
        )

        # Update KPI Card 4: LLM Engine
        loc_m = self.vars["local_model"].get().split("/")[-1][:18]
        self.kpi_cards["engine"]["val"].configure(text=loc_m or "Local + Web")
        self.kpi_cards["engine"]["sub"].configure(
            text=f"Temp {self.vars['temperature'].get()} · Think {self.vars['thinking_tokens'].get()}"
        )

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
    DianaControlCenterApp().mainloop()
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

        sidebar_w = 68 if collapsed else 232
        ws_w = max(win_w - sidebar_w, 360)

        kpi_cols = 2 if ws_w < cls.COMPACT_WORKSPACE_W else 4
        dual_forms = ws_w >= cls.COMPACT_WORKSPACE_W
        compact_tb = ws_w < 860

        changed = (
            bp != current.breakpoint
            or collapsed != current.sidebar_collapsed
            or kpi_cols != current.kpi_columns
            or dual_forms != current.dual_col_forms
            or compact_tb != current.compact_toolbar
            or abs(ws_w - current.workspace_width) >= 24
        )

        new_state = ViewportLayoutState(
            window_width=win_w,
            window_height=win_h,
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

    @staticmethod
    def blank_persona_template() -> Dict[str, Any]:
        return {
            "name": "",
            "age_style": "",
            "background": "",
            "relationship": "",
            "guest_attitude": "",
            "personality_traits": [""],
            "speech_patterns": [""],
            "tone_rules": [""],
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

        return {
            "bot_version": version_data.get("bot", "—"),
            "persona_version": version_data.get("character_persona", "—"),
            "persona_name": persona_data.get("name", "未設定") if isinstance(persona_data, dict) else "未設定",
            "persona_age_style": persona_data.get("age_style", "") if isinstance(persona_data, dict) else "",
            "user_count": user_count,
            "max_favor": max_favor,
            "reminder_count": reminder_count,
            "action_img_count": action_img_count,
            "last_message_time": bot_state.get("last_message_time", "無紀錄"),
            "runtime": runtime_status,
        }


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
        if not PATCH_PUSHER_SCRIPT.exists():
            return False
        exe_dir = Path(sys.executable).resolve().parent
        pythonw = exe_dir / "pythonw.exe"
        runner = str(pythonw) if (sys.platform.startswith("win") and pythonw.exists()) else sys.executable
        subprocess.Popen([runner, str(PATCH_PUSHER_SCRIPT)], cwd=str(ROOT))
        return True

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
                    text=f"\n[{datetime.now().strftime('%H:%M:%S')}] === 📦 開始安裝 requirements.txt 依賴套件 ===\n",
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
                self.emit("DEPS_INSTALL_FINISHED", returncode=proc.returncode)
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

            # 1. Reflow Collapsible Sidebar Rail (68px icon mode vs 232px full mode)
            target_sb_w = 68 if vp.sidebar_collapsed else 232
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
                    if not widgets["desc"].winfo_ismapped():
                        widgets["desc"].pack(fill="x", pady=(2, 0))
                    widgets["inner"].configure(padx=12, pady=9)

            # 2. Reflow Dashboard 4 KPI Cards (1x4 wide vs 2x2 compact)
            cols = vp.kpi_columns
            for c in range(4):
                self.kpi_row.columnconfigure(c, weight=1 if c < cols else 0, uniform="kpi" if c < cols else "")

            card_wrap_px = max((vp.workspace_width - 64) // max(cols, 1) - 28, 130)
            for idx, key in enumerate(("status", "persona", "social", "engine")):
                card_info = self.kpi_cards[key]
                frame = card_info["frame"]
                r = idx // cols
                c = idx % cols
                pad_l = 0 if c == 0 else 5
                pad_r = 0 if c == (cols - 1) else 5
                pad_b = 6 if (cols == 2 and r == 0) else 0
                frame.grid_configure(row=r, column=c, sticky="nsew", padx=(pad_l, pad_r), pady=(0, pad_b))
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
        # Left Collapsible Sidebar Navigation Rail
        self.sidebar = tk.Frame(
            self,
            bg=PALETTE["bg_sidebar"],
            width=232,
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

        if PATCH_PUSHER_SCRIPT.exists():
            ModernButton(
                self.topbar,
                "🩹 開啟補丁推送工具",
                self.action_open_patch_pusher,
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
            for pid in ("dashboard", "connections", "persona", "system")
        }

        self._build_dashboard_page()
        self._build_connections_page()
        self._build_persona_page()
        self._build_system_page()
        self.switch_page("dashboard")

    def switch_page(self, page_id: str) -> None:
        self.lifecycle.active_page = page_id
        titles = {
            "dashboard": "即時儀表板 (Runtime Dashboard)",
            "connections": "連線與獨立憑證設定 (API & Credentials)",
            "persona": "角色設定工坊 (Persona Studio)",
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
        kpi_defs = [
            ("status", "執行狀態 (RUNTIME)", "OFFLINE", "尚未啟動 bot.py"),
            ("persona", "目前角色 (PERSONA)", "黛安娜", "Bot v— · Persona v—"),
            ("social", "關係與排程 (MEMORY)", "0 位互動對象", "最高好感 0 · 提醒 0 筆"),
            ("engine", "推理引擎 (LLM ENGINE)", "Local + Web", "Temp 0.75 · Think 256"),
        ]
        for idx, (key, title, val, sub) in enumerate(kpi_defs):
            c = tk.Frame(
                self.kpi_row,
                bg=PALETTE["bg_card"],
                highlightthickness=1,
                highlightbackground=PALETTE["border"],
                padx=14,
                pady=10,
            )
            c.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 5, 0 if idx == 3 else 5))
            tk.Label(
                c,
                text=title,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_muted"],
                font=("Segoe UI", 8, "bold"),
                anchor="w",
            ).pack(fill="x")
            lbl_val = tk.Label(
                c,
                text=val,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_primary"],
                font=("Microsoft JhengHei UI", 13, "bold"),
                anchor="w",
            )
            lbl_val.pack(fill="x", pady=(3, 1))
            lbl_sub = tk.Label(
                c,
                text=sub,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_secondary"],
                font=("Microsoft JhengHei UI", 9),
                anchor="w",
                justify="left",
            )
            lbl_sub.pack(fill="x")
            self.kpi_cards[key] = {"frame": c, "val": lbl_val, "sub": lbl_sub}

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
            subtitle="適用 LM Studio / Ollama / vLLM 等 OpenAI 相容端點",
        )
        c_local.pack(fill="x")
        self._create_labeled_entry(c_local, "Base URL", self.vars["local_base_url"], "例: http://127.0.0.1:1234/v1")
        self._create_labeled_entry(c_local, "Model Name", self.vars["local_model"], "例: google/gemma-4-e2b")
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

        ModernButton(btn_row, "💾 儲存 Persona 設定", self.save_persona, variant="primary", padx=15, pady=6).pack(side="right")

        # Mode Switcher (Visual Form vs Raw JSON)
        self.persona_mode = "visual"
        self.btn_mode_json = ModernButton(btn_row, "{ } JSON 原始碼模式", lambda: self.switch_persona_editor_mode("json"), variant="ghost", padx=11, pady=6)
        self.btn_mode_json.pack(side="right", padx=5)
        self.btn_mode_visual = ModernButton(btn_row, "📝 視覺化表單模式", lambda: self.switch_persona_editor_mode("visual"), variant="secondary", padx=11, pady=6)
        self.btn_mode_visual.pack(side="right", padx=5)

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

        self.p_bg_text = _make_text_box(self.p_left_col, "角色背景設定 (background)", 4)
        self.p_rel_text = _make_text_box(self.p_left_col, "與主人的關係 (relationship)", 3)
        self.p_guest_text = _make_text_box(self.p_left_col, "對待訪客態度 (guest_attitude)", 3)
        self.p_favor_text = _make_text_box(self.p_left_col, "好感度三階段反應 (低 / 中 / 高，每行一段)", 4)

        self.p_traits_text = _make_text_box(self.p_right_col, "性格特質 (personality_traits — 每行一條)", 5)
        self.p_speech_text = _make_text_box(self.p_right_col, "說話習慣 (speech_patterns — 每行一條)", 5)
        self.p_tone_text = _make_text_box(self.p_right_col, "語氣與行為規範 (tone_rules — 每行一條)", 5)

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

        fav = obj.get("favorability_reactions", {})
        if not isinstance(fav, dict):
            fav = {}
        fav_lines = [
            f"[低] {fav.get('low', '')}",
            f"[中] {fav.get('medium', '')}",
            f"[高] {fav.get('high', '')}",
        ]
        _set_txt(self.p_favor_text, "\n".join(fav_lines))

        _set_txt(self.p_traits_text, "\n".join(str(x) for x in obj.get("personality_traits", []) if str(x).strip()))
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
            return items or [""]

        base_obj["personality_traits"] = _lines(self.p_traits_text)
        base_obj["speech_patterns"] = _lines(self.p_speech_text)
        base_obj["tone_rules"] = _lines(self.p_tone_text)

        fav_raw = [line.strip() for line in self.p_favor_text.get("1.0", "end").splitlines() if line.strip()]
        fav_dict = {"low": "", "medium": "", "high": ""}
        for idx, key in enumerate(("low", "medium", "high")):
            if idx < len(fav_raw):
                val = fav_raw[idx]
                for prefix in ("[低]", "[中]", "[高]"):
                    if val.startswith(prefix):
                        val = val[len(prefix):].strip()
                fav_dict[key] = val
        base_obj["favorability_reactions"] = fav_dict
        return base_obj

    def switch_persona_editor_mode(self, target_mode: str) -> None:
        if target_mode == self.persona_mode:
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
                return
            self.persona_json_frame.pack_forget()
            self.persona_scroll_host.pack(fill="both", expand=True)
        self.persona_mode = target_mode

    def _set_persona_object(self, obj: Dict[str, Any]) -> None:
        self.persona_text.delete("1.0", "end")
        self.persona_text.insert("1.0", json.dumps(obj, ensure_ascii=False, indent=2))
        self._sync_json_to_visual_form(obj)

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
            subtitle="自動驗證設定檔、獨立金鑰與參考圖資源狀態（支援視窗寬度自適應排版）",
        )
        diag_card.pack(fill="x", pady=(0, 12))

        self.diag_grid = tk.Frame(diag_card, bg=PALETTE["bg_card"])
        self.diag_grid.pack(fill="x", pady=(0, 10))
        self.diag_cells: List[tk.Frame] = []

        diag_btn_row = tk.Frame(diag_card, bg=PALETTE["bg_card"])
        diag_btn_row.pack(fill="x")

        self.btn_install_deps = ModernButton(
            diag_btn_row,
            "📦 安裝 / 更新 requirements.txt",
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

    def refresh_file_diagnostics(self) -> None:
        for child in self.diag_grid.winfo_children():
            child.destroy()
        self.diag_cells.clear()

        ref_count = len(list(REFERENCE_IMG_DIR.glob("*.*"))) if REFERENCE_IMG_DIR.exists() else 0
        checks = [
            ("核心主程式 (bot.py)", (ROOT / "bot.py").exists(), "bot.py"),
            ("演化引擎 (diana_evolution.py)", (ROOT / "diana_evolution.py").exists(), "diana_evolution.py"),
            ("角色設定檔 (character_persona.json)", PERSONA.exists(), "config/character_persona.json"),
            ("Discord Bot Token", bool(ConfigRepository.read_first_line(TOKEN)), "config/discord_token.txt"),
            ("Chat API Key (聊天專用)", bool(ConfigRepository.read_first_line(CHAT_KEY)), "config/google_api_key_chat.txt"),
            ("Evolution API Key (演化專用)", bool(ConfigRepository.read_first_line(EVOLUTION_KEY)), "config/google_api_key_evolution.txt"),
            ("Image API Key (生圖專用)", bool(ConfigRepository.read_first_line(IMAGE_KEY)), "config/google_api_key_image.txt"),
            (f"角色參考圖庫 ({ref_count} 張)", ref_count > 0, "img/reference/"),
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
            badge_txt = "✓ 已就緒" if ok else "⚠ 待設定"
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

        self.vars["discord"].set(ConfigRepository.read_first_line(TOKEN))
        self.vars["chat_key"].set(ConfigRepository.read_first_line(CHAT_KEY))
        self.vars["evolution_key"].set(ConfigRepository.read_first_line(EVOLUTION_KEY))
        self.vars["evolution_model"].set(ConfigRepository.read_second_line(EVOLUTION_KEY))
        self.vars["image_key"].set(ConfigRepository.read_first_line(IMAGE_KEY))
        self.vars["temperature"].set(str(self.cfg.get("temperature", 0.75)))
        self.vars["thinking_tokens"].set(str(self.cfg.get("thinking_tokens", 256)))

        self.load_current_persona_file(silent=True)
        self.refresh_file_diagnostics()
        self._refresh_dashboard_telemetry()

    def save_all_settings(self, silent: bool = False) -> bool:
        try:
            for k in ["local_base_url", "local_model", "web_base_url", "web_model", "image_endpoint", "image_model"]:
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

    def save_persona(self) -> None:
        try:
            if self.persona_mode == "visual":
                obj = self._collect_visual_form_to_dict()
                self.persona_text.delete("1.0", "end")
                self.persona_text.insert("1.0", json.dumps(obj, ensure_ascii=False, indent=2))
            else:
                obj = json.loads(self.persona_text.get("1.0", "end"))
                self._sync_json_to_visual_form(obj)

            ConfigRepository.write_json(PERSONA, obj)
            self._refresh_dashboard_telemetry()
            self.show_toast(f"✓ 角色「{obj.get('name', '未命名')}」已儲存至 config/character_persona.json", level="success")
        except Exception as e:
            messagebox.showerror("Persona 儲存失敗", f"JSON 格式無效或寫入失敗：\n{e}")

    # -----------------------------------------------------------------------
    # Worker Trigger Actions
    # -----------------------------------------------------------------------
    def action_open_patch_pusher(self) -> None:
        if self.workers.launch_patch_pusher():
            self.show_toast("🩹 已開啟 Diana Patch Pusher 補丁推送工具", level="info")
        else:
            self.show_toast("⚠ 找不到 patch_pusher.py", level="warn")

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
                    self.show_toast("📦 正在執行 pip install -r requirements.txt...", level="info")

                elif stype == "DEPS_INSTALL_FINISHED":
                    self.btn_install_deps.set_enabled(True, "📦 安裝 / 更新 requirements.txt")
                    rc = p.get("returncode", -1)
                    if rc == 0:
                        self.show_toast("✓ requirements.txt 依賴套件安裝完成", level="success")
                    else:
                        self.show_toast(f"⚠ 套件安裝結束 (代碼 {rc})，請查看日誌", level="warn")

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

        # Update KPI Card 2: Persona & Version
        self.kpi_cards["persona"]["val"].configure(text=snap["persona_name"])
        self.kpi_cards["persona"]["sub"].configure(
            text=f"Bot v{snap['bot_version']} · Persona v{snap['persona_version']}"
        )

        # Update KPI Card 3: Social & Memory
        self.kpi_cards["social"]["val"].configure(text=f"{snap['user_count']} 位互動對象")
        self.kpi_cards["social"]["sub"].configure(
            text=f"最高好感 {snap['max_favor']} · 提醒 {snap['reminder_count']} 筆 · 插圖 {snap['action_img_count']}"
        )

        # Update KPI Card 4: LLM Engine
        loc_m = self.vars["local_model"].get().split("/")[-1][:18]
        self.kpi_cards["engine"]["val"].configure(text=loc_m or "Local + Web")
        self.kpi_cards["engine"]["sub"].configure(
            text=f"Temp {self.vars['temperature'].get()} · Think {self.vars['thinking_tokens'].get()}"
        )

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
    DianaControlCenterApp().mainloop()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 1. State Management & Broadcast Signal Layer
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class BroadcastSignal:
    """Explicit signal contract between background execution workers and UI orchestrator."""
    signal_type: str
    payload: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class RuntimeLifecycleState:
    """
    Centralized state tracker for asynchronous tasks and subprocess lifecycles.
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
    active_page: str = "dashboard"


# ---------------------------------------------------------------------------
# 2. Data & Configuration Repository Layer
# ---------------------------------------------------------------------------
class ConfigRepository:
    """Encapsulates all file I/O with atomic writes and credential separation."""

    @staticmethod
    def ensure_dirs() -> None:
        CONFIG.mkdir(parents=True, exist_ok=True)
        DATA.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def read_json(path: Path, default: Any) -> Any:
        try:
=======
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
    workspace_width: int = 985
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

        sidebar_w = 68 if collapsed else 232
        ws_w = max(win_w - sidebar_w, 360)

        kpi_cols = 2 if ws_w < cls.COMPACT_WORKSPACE_W else 4
        dual_forms = ws_w >= cls.COMPACT_WORKSPACE_W
        compact_tb = ws_w < 860

        changed = (
            bp != current.breakpoint
            or collapsed != current.sidebar_collapsed
            or kpi_cols != current.kpi_columns
            or dual_forms != current.dual_col_forms
            or compact_tb != current.compact_toolbar
            or abs(ws_w - current.workspace_width) >= 24
        )

        new_state = ViewportLayoutState(
            window_width=win_w,
            window_height=win_h,
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

    @staticmethod
    def read_json(path: Path, default: Any) -> Any:
        try:
>>>>>>> theirs
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
<<<<<<< ours
    def write_first_line_preserve_rest(path: Path, first_line_val: str, second_line_override: Optional[str] = None) -> None:
=======
    def write_first_line_preserve_rest(
        path: Path,
        first_line_val: str,
        second_line_override: Optional[str] = None,
    ) -> None:
>>>>>>> theirs
        """
        Writes the first-line API key while preserving any subsequent lines
        (such as the evolution model stored on line 2 of google_api_key_evolution.txt).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        existing_lines: List[str] = []
        if path.exists():
<<<<<<< ours
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

    @staticmethod
    def blank_persona_template() -> Dict[str, Any]:
        return {
            "name": "",
            "age_style": "",
            "background": "",
            "relationship": "",
            "guest_attitude": "",
            "personality_traits": [""],
            "speech_patterns": [""],
            "tone_rules": [""],
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

        return {
            "bot_version": version_data.get("bot", "—"),
            "persona_version": version_data.get("character_persona", "—"),
            "persona_name": persona_data.get("name", "未設定") if isinstance(persona_data, dict) else "未設定",
            "persona_age_style": persona_data.get("age_style", "") if isinstance(persona_data, dict) else "",
            "user_count": user_count,
            "max_favor": max_favor,
            "reminder_count": reminder_count,
            "action_img_count": action_img_count,
            "last_message_time": bot_state.get("last_message_time", "無紀錄"),
            "runtime": runtime_status,
        }


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

    # --- Bot Process Lifecycle ---
    def is_bot_alive(self) -> bool:
        alive = bool(self.bot_proc and self.bot_proc.poll() is None)
        self.state.is_bot_running_flag = alive
        return alive

    def start_bot_process(self) -> bool:
        if self.is_bot_alive():
            return False
        self.state.is_bot_running_flag = True
        self.emit("LOG_APPEND", text=f"\n[{datetime.now().strftime('%H:%M:%S')}] === 🚀 正在啟動 Diana Bot (bot.py) ===\n", tag="system")
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
            self.emit("LOG_APPEND", text=f"[{datetime.now().strftime('%H:%M:%S')}] === ⏹ Diana 行程已結束 (exit code: {rc}) ===\n", tag="system")
            self.emit("BOT_PROCESS_EXITED", returncode=rc)

    def stop_bot_process(self) -> bool:
        if not self.is_bot_alive() or not self.bot_proc:
            self.state.is_bot_running_flag = False
            return False
        try:
            self.bot_proc.terminate()
            self.emit("LOG_APPEND", text=f"[{datetime.now().strftime('%H:%M:%S')}] === 🛑 已發送停止訊號予 Diana ===\n", tag="warn")
            return True
        except Exception as e:
            self.emit("LOG_APPEND", text=f"❌ 停止程序時發生異常: {e}\n", tag="error")
            return False

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
                    "User-Agent": "Diana-Control-Center/2.0",
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
                # Deterministic lifecycle reset
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
                        "User-Agent": "Diana-Control-Center/2.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                text = data["choices"][0]["message"]["content"].strip()
                # Strip optional <think>...</think> blocks or markdown code fences
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
                # Deterministic lifecycle reset
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
                self.emit("LOG_APPEND", text=f"\n[{datetime.now().strftime('%H:%M:%S')}] === 📦 開始安裝 requirements.txt 依賴套件 ===\n", tag="system")
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
                self.emit(
                    "DEPS_INSTALL_FINISHED",
                    returncode=proc.returncode,
                )
            except Exception as e:
                self.emit("LOG_APPEND", text=f"❌ 套件安裝失敗: {e}\n", tag="error")
                self.emit("DEPS_INSTALL_FINISHED", returncode=-1, error=str(e))
            finally:
                # Deterministic lifecycle reset
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
                # Deterministic lifecycle reset
                self.state.is_running_evolution_flag = False

        threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# 4. Custom Modern UI Widgets (Pure Tkinter + Vector Canvas)
# ---------------------------------------------------------------------------
class PipelineStepperCanvas(tk.Canvas):
    """
    Vector-rendered pipeline stepper visualizing Diana's 6 runtime stages:
    等待待命 -> 訊息分析 -> 記憶檢索 -> LLM 思考 -> 訊號回覆 -> 整理記憶
    with active glow rings, status colors, and smooth responsive layout.
    """

    def __init__(self, parent: tk.Widget, **kwargs: Any) -> None:
        super().__init__(
            parent,
            height=96,
            bg=PALETTE["bg_card"],
            highlightthickness=0,
            bd=0,
            **kwargs,
        )
        self.active_stage: str = ""
        self.is_online: bool = False
        self.pulse_phase: int = 0
        self.bind("<Configure>", lambda _e: self.redraw())

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
        w = max(self.winfo_width(), 640)
        n = len(PIPELINE_STAGES)
        pad_x = 58
        usable_w = max(w - pad_x * 2, 300)
        step_w = usable_w / max(n - 1, 1)
        cy = 36

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
                halo_r = 21 if self.pulse_phase == 0 else 24
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
                    cx - 13,
                    cy - 13,
                    cx + 13,
                    cy + 13,
                    fill=node_color,
                    outline="#FFFFFF",
                    width=2,
                )
                self.create_text(cx, cy, text="●", fill="#FFFFFF", font=("Segoe UI", 9, "bold"))
                label_color = "#FFFFFF"
                sub_color = PALETTE["cyan"] if key != "idle" else PALETTE["success"]
            elif is_passed:
                self.create_oval(
                    cx - 11,
                    cy - 11,
                    cx + 11,
                    cy + 11,
                    fill=PALETTE["accent_soft"],
                    outline=PALETTE["accent"],
                    width=2,
                )
                self.create_text(cx, cy, text="✓", fill=PALETTE["accent"], font=("Segoe UI", 9, "bold"))
                label_color = PALETTE["text_primary"]
                sub_color = PALETTE["text_secondary"]
            else:
                self.create_oval(
                    cx - 11,
                    cy - 11,
                    cx + 11,
                    cy + 11,
                    fill=PALETTE["bg_input"],
                    outline="#334155",
                    width=2,
                )
                self.create_text(cx, cy, text=str(i + 1), fill=PALETTE["text_muted"], font=("Segoe UI", 8, "bold"))
                label_color = PALETTE["text_secondary"] if self.is_online else PALETTE["text_muted"]
                sub_color = PALETTE["text_muted"]

            self.create_text(
                cx,
                cy + 28,
                text=title_zh,
                fill=label_color,
                font=("Microsoft JhengHei UI", 10, "bold" if is_current else "normal"),
            )
            self.create_text(
                cx,
                cy + 45,
                text=subtitle_en,
                fill=sub_color,
                font=("Segoe UI", 8),
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


# ---------------------------------------------------------------------------
# 5. Main Presentation Layer (DianaControlCenterApp)
# ---------------------------------------------------------------------------
class DianaControlCenterApp(tk.Tk):
    """Modernized Tkinter desktop application for Diana Bot Control Center."""

    def __init__(self) -> None:
        enable_high_dpi()
        super().__init__()
        os.chdir(ROOT)
        ConfigRepository.ensure_dirs()

        # Core architectural state & worker bus
        self.signal_queue: "queue.Queue[BroadcastSignal]" = queue.Queue()
        self.lifecycle = RuntimeLifecycleState()
        self.workers = ExecutionWorkers(self.signal_queue, self.lifecycle)
        self.cfg: Dict[str, Any] = ConfigRepository.load_gui_config()

        # Window setup
        self.title("Diana Bot Control Center — 智慧中控台")
        self.geometry("1220x800")
        self.minsize(1040, 680)
        self.configure(bg=PALETTE["bg_app"])

        # Tkinter reactive variables
        self.vars: Dict[str, tk.StringVar] = {
            k: tk.StringVar()
            for k in [
                "local_base_url",
                "local_model",
                "web_base_url",
                "web_model",
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

        self._configure_ttk_styles()
        self._build_shell()
        self.load_all_settings()

        # Start event loop tickers
        self.after(200, self._tick_loop)
        self.protocol("WM_DELETE_WINDOW", self._on_close_window)

        if not GUI_CONFIG.exists():
            self.after(500, self._show_first_run_welcome)
=======
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

    @staticmethod
    def blank_persona_template() -> Dict[str, Any]:
        return {
            "name": "",
            "age_style": "",
            "background": "",
            "relationship": "",
            "guest_attitude": "",
            "personality_traits": [""],
            "speech_patterns": [""],
            "tone_rules": [""],
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

        return {
            "bot_version": version_data.get("bot", "—"),
            "persona_version": version_data.get("character_persona", "—"),
            "persona_name": persona_data.get("name", "未設定") if isinstance(persona_data, dict) else "未設定",
            "persona_age_style": persona_data.get("age_style", "") if isinstance(persona_data, dict) else "",
            "user_count": user_count,
            "max_favor": max_favor,
            "reminder_count": reminder_count,
            "action_img_count": action_img_count,
            "last_message_time": bot_state.get("last_message_time", "無紀錄"),
            "runtime": runtime_status,
        }


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
        if not PATCH_PUSHER_SCRIPT.exists():
            return False
        exe_dir = Path(sys.executable).resolve().parent
        pythonw = exe_dir / "pythonw.exe"
        runner = str(pythonw) if (sys.platform.startswith("win") and pythonw.exists()) else sys.executable
        subprocess.Popen([runner, str(PATCH_PUSHER_SCRIPT)], cwd=str(ROOT))
        return True

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
                    text=f"\n[{datetime.now().strftime('%H:%M:%S')}] === 📦 開始安裝 requirements.txt 依賴套件 ===\n",
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
                self.emit("DEPS_INSTALL_FINISHED", returncode=proc.returncode)
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

            # 1. Reflow Collapsible Sidebar Rail (68px icon mode vs 232px full mode)
            target_sb_w = 68 if vp.sidebar_collapsed else 232
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
                    if not widgets["desc"].winfo_ismapped():
                        widgets["desc"].pack(fill="x", pady=(2, 0))
                    widgets["inner"].configure(padx=12, pady=9)

            # 2. Reflow Dashboard 4 KPI Cards (1x4 wide vs 2x2 compact)
            cols = vp.kpi_columns
            for c in range(4):
                self.kpi_row.columnconfigure(c, weight=1 if c < cols else 0, uniform="kpi" if c < cols else "")

            card_wrap_px = max((vp.workspace_width - 64) // max(cols, 1) - 28, 130)
            for idx, key in enumerate(("status", "persona", "social", "engine")):
                card_info = self.kpi_cards[key]
                frame = card_info["frame"]
                r = idx // cols
                c = idx % cols
                pad_l = 0 if c == 0 else 5
                pad_r = 0 if c == (cols - 1) else 5
                pad_b = 6 if (cols == 2 and r == 0) else 0
                frame.grid_configure(row=r, column=c, sticky="nsew", padx=(pad_l, pad_r), pady=(0, pad_b))
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
>>>>>>> theirs

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

<<<<<<< ours
    def _create_card(self, parent: tk.Widget, title: str = "", subtitle: str = "", pad: int = 18) -> tk.Frame:
=======
    def _create_card(self, parent: tk.Widget, title: str = "", subtitle: str = "", pad: int = 16) -> tk.Frame:
>>>>>>> theirs
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
<<<<<<< ours
            hdr.pack(fill="x", pady=(0, 12))
=======
            hdr.pack(fill="x", pady=(0, 10))
>>>>>>> theirs
            tk.Label(
                hdr,
                text=title,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_primary"],
<<<<<<< ours
                font=("Microsoft JhengHei UI", 12, "bold"),
=======
                font=("Microsoft JhengHei UI", 11, "bold"),
>>>>>>> theirs
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
<<<<<<< ours
        # Left Sidebar Navigation Rail
        self.sidebar = tk.Frame(
            self,
            bg=PALETTE["bg_sidebar"],
            width=235,
=======
        # Left Collapsible Sidebar Navigation Rail
        self.sidebar = tk.Frame(
            self,
            bg=PALETTE["bg_sidebar"],
            width=232,
>>>>>>> theirs
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
        )
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

<<<<<<< ours
        # Brand Header
        brand_box = tk.Frame(self.sidebar, bg=PALETTE["bg_sidebar"], padx=20, pady=22)
        brand_box.pack(fill="x")
        title_row = tk.Frame(brand_box, bg=PALETTE["bg_sidebar"])
        title_row.pack(fill="x")
=======
        # Brand Header + Collapse Toggle Button
        brand_box = tk.Frame(self.sidebar, bg=PALETTE["bg_sidebar"], padx=14, pady=18)
        brand_box.pack(fill="x")
        title_row = tk.Frame(brand_box, bg=PALETTE["bg_sidebar"])
        title_row.pack(fill="x")

>>>>>>> theirs
        self.brand_dot = tk.Label(
            title_row,
            text="●",
            bg=PALETTE["bg_sidebar"],
            fg=PALETTE["text_muted"],
<<<<<<< ours
            font=("Segoe UI", 14),
        )
        self.brand_dot.pack(side="left", padx=(0, 8))
        tk.Label(
=======
            font=("Segoe UI", 13),
        )
        self.brand_dot.pack(side="left", padx=(0, 6))

        self.brand_text_lbl = tk.Label(
>>>>>>> theirs
            title_row,
            text="DIANA",
            bg=PALETTE["bg_sidebar"],
            fg=PALETTE["text_primary"],
<<<<<<< ours
            font=("Segoe UI", 19, "bold"),
        ).pack(side="left")
=======
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

>>>>>>> theirs
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
<<<<<<< ours
        tk.Label(
=======

        self.brand_subtitle_lbl = tk.Label(
>>>>>>> theirs
            brand_box,
            text="Persistent AI Companion 中控台",
            bg=PALETTE["bg_sidebar"],
            fg=PALETTE["text_secondary"],
<<<<<<< ours
            font=("Microsoft JhengHei UI", 9),
        ).pack(anchor="w", pady=(4, 0))

        # Navigation Items
        nav_items = [
            ("dashboard", "📊  即時儀表板", "狀態監控、管線與執行日誌"),
            ("connections", "🔌  連線與金鑰", "Local / Web / Image API 設定"),
            ("persona", "🎭  角色設定工坊", "視覺化表單、JSON 與 AI 生成"),
            ("system", "🛠️  系統診斷與演化", "環境檢查、參數與主動演化"),
        ]
        self.nav_buttons: Dict[str, Dict[str, tk.Widget]] = {}
        nav_container = tk.Frame(self.sidebar, bg=PALETTE["bg_sidebar"], padx=10)
        nav_container.pack(fill="x", pady=6)

        for page_id, title_text, desc_text in nav_items:
=======
            font=("Microsoft JhengHei UI", 8),
        )
        self.brand_subtitle_lbl.pack(anchor="w", pady=(4, 0))

        # Navigation Items
        nav_items = [
            ("dashboard", "📊", "📊  即時儀表板", "狀態監控、管線與執行日誌"),
            ("connections", "🔌", "🔌  連線與金鑰", "Local / Web / Image API 設定"),
            ("persona", "🎭", "🎭  角色設定工坊", "視覺化表單、JSON 與 AI 生成"),
            ("system", "🛠️", "🛠️  系統診斷與演化", "環境檢查、參數與主動演化"),
        ]
        self.nav_buttons: Dict[str, Dict[str, Any]] = {}
        nav_container = tk.Frame(self.sidebar, bg=PALETTE["bg_sidebar"], padx=8)
        nav_container.pack(fill="x", pady=4)

        for page_id, icon_only, title_text, desc_text in nav_items:
>>>>>>> theirs
            item_frame = tk.Frame(nav_container, bg=PALETTE["bg_sidebar"], cursor="hand2")
            item_frame.pack(fill="x", pady=3)
            indicator = tk.Frame(item_frame, bg=PALETTE["bg_sidebar"], width=4)
            indicator.pack(side="left", fill="y")
<<<<<<< ours
            inner = tk.Frame(item_frame, bg=PALETTE["bg_sidebar"], padx=12, pady=10)
=======
            inner = tk.Frame(item_frame, bg=PALETTE["bg_sidebar"], padx=12, pady=9)
>>>>>>> theirs
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
<<<<<<< ours
=======
                "icon_only": icon_only,
                "full_title": title_text,
>>>>>>> theirs
            }
            for w in (item_frame, indicator, inner, lbl_title, lbl_desc):
                w.bind("<Button-1>", lambda _e, pid=page_id: self.switch_page(pid))

        # Sidebar Footer Quick Summary
<<<<<<< ours
        footer = tk.Frame(
=======
        self.sidebar_footer = tk.Frame(
>>>>>>> theirs
            self.sidebar,
            bg=PALETTE["bg_card"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=12,
<<<<<<< ours
            pady=12,
        )
        footer.pack(side="bottom", fill="x", padx=12, pady=14)
        self.sidebar_status_lbl = tk.Label(
            footer,
=======
            pady=10,
        )
        self.sidebar_footer.pack(side="bottom", fill="x", padx=12, pady=12)
        self.sidebar_status_lbl = tk.Label(
            self.sidebar_footer,
>>>>>>> theirs
            text="狀態：OFFLINE (未啟動)",
            bg=PALETTE["bg_card"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
            anchor="w",
        )
        self.sidebar_status_lbl.pack(fill="x")
        self.sidebar_sub_lbl = tk.Label(
<<<<<<< ours
            footer,
=======
            self.sidebar_footer,
>>>>>>> theirs
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

<<<<<<< ours
        # Top Bar (Page Header + Non-blocking Toast Notification)
        self.topbar = tk.Frame(self.workspace, bg=PALETTE["bg_app"], padx=24, pady=16)
=======
        # Top Bar (Page Header + Patch Pusher Quick Button + Non-blocking Toast Notification)
        self.topbar = tk.Frame(self.workspace, bg=PALETTE["bg_app"], padx=20, pady=12)
>>>>>>> theirs
        self.topbar.pack(fill="x")
        self.page_title_lbl = tk.Label(
            self.topbar,
            text="即時儀表板",
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_primary"],
<<<<<<< ours
            font=("Microsoft JhengHei UI", 18, "bold"),
        )
        self.page_title_lbl.pack(side="left")

=======
            font=("Microsoft JhengHei UI", 16, "bold"),
        )
        self.page_title_lbl.pack(side="left")

        if PATCH_PUSHER_SCRIPT.exists():
            ModernButton(
                self.topbar,
                "🩹 開啟補丁推送工具",
                self.action_open_patch_pusher,
                variant="secondary",
                padx=12,
                pady=5,
            ).pack(side="right", padx=(8, 0))

>>>>>>> theirs
        self.toast_lbl = tk.Label(
            self.topbar,
            text="",
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 9, "bold"),
<<<<<<< ours
            padx=14,
            pady=6,
=======
            padx=12,
            pady=5,
>>>>>>> theirs
        )
        self.toast_lbl.pack(side="right")

        # Content Pages Container
<<<<<<< ours
        self.pages_container = tk.Frame(self.workspace, bg=PALETTE["bg_app"], padx=24, pady=4)
        self.pages_container.pack(fill="both", expand=True, pady=(0, 18))
=======
        self.pages_container = tk.Frame(self.workspace, bg=PALETTE["bg_app"], padx=20, pady=2)
        self.pages_container.pack(fill="both", expand=True, pady=(0, 14))
>>>>>>> theirs

        self.pages: Dict[str, tk.Frame] = {
            pid: tk.Frame(self.pages_container, bg=PALETTE["bg_app"])
            for pid in ("dashboard", "connections", "persona", "system")
        }

        self._build_dashboard_page()
        self._build_connections_page()
        self._build_persona_page()
        self._build_system_page()
        self.switch_page("dashboard")

    def switch_page(self, page_id: str) -> None:
        self.lifecycle.active_page = page_id
        titles = {
            "dashboard": "即時儀表板 (Runtime Dashboard)",
            "connections": "連線與獨立憑證設定 (API & Credentials)",
            "persona": "角色設定工坊 (Persona Studio)",
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
<<<<<<< ours
=======
        self.after(20, lambda: self._apply_responsive_reflow(force=True))
>>>>>>> theirs

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
<<<<<<< ours
    # Page 1: Dashboard (KPI Cards + Pipeline Stepper + Action Bar + Console)
=======
    # Page 1: Responsive Dashboard (Reflowing KPI Grid + Draggable Split Pane)
>>>>>>> theirs
    # -----------------------------------------------------------------------
    def _build_dashboard_page(self) -> None:
        p = self.pages["dashboard"]

<<<<<<< ours
        # Row 1: 4 Telemetry KPI Cards
        kpi_row = tk.Frame(p, bg=PALETTE["bg_app"])
        kpi_row.pack(fill="x", pady=(0, 12))
        for col in range(4):
            kpi_row.columnconfigure(col, weight=1, uniform="kpi")

        self.kpi_cards: Dict[str, Dict[str, tk.Label]] = {}
=======
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
>>>>>>> theirs
        kpi_defs = [
            ("status", "執行狀態 (RUNTIME)", "OFFLINE", "尚未啟動 bot.py"),
            ("persona", "目前角色 (PERSONA)", "黛安娜", "Bot v— · Persona v—"),
            ("social", "關係與排程 (MEMORY)", "0 位互動對象", "最高好感 0 · 提醒 0 筆"),
            ("engine", "推理引擎 (LLM ENGINE)", "Local + Web", "Temp 0.75 · Think 256"),
        ]
        for idx, (key, title, val, sub) in enumerate(kpi_defs):
            c = tk.Frame(
<<<<<<< ours
                kpi_row,
=======
                self.kpi_row,
>>>>>>> theirs
                bg=PALETTE["bg_card"],
                highlightthickness=1,
                highlightbackground=PALETTE["border"],
                padx=14,
<<<<<<< ours
                pady=12,
            )
            c.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 6, 0 if idx == 3 else 6))
=======
                pady=10,
            )
            c.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 5, 0 if idx == 3 else 5))
>>>>>>> theirs
            tk.Label(
                c,
                text=title,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_muted"],
                font=("Segoe UI", 8, "bold"),
                anchor="w",
            ).pack(fill="x")
            lbl_val = tk.Label(
                c,
                text=val,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_primary"],
<<<<<<< ours
                font=("Microsoft JhengHei UI", 14, "bold"),
                anchor="w",
            )
            lbl_val.pack(fill="x", pady=(4, 2))
=======
                font=("Microsoft JhengHei UI", 13, "bold"),
                anchor="w",
            )
            lbl_val.pack(fill="x", pady=(3, 1))
>>>>>>> theirs
            lbl_sub = tk.Label(
                c,
                text=sub,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_secondary"],
                font=("Microsoft JhengHei UI", 9),
                anchor="w",
<<<<<<< ours
            )
            lbl_sub.pack(fill="x")
            self.kpi_cards[key] = {"val": lbl_val, "sub": lbl_sub}

        # Row 2: Visual Pipeline Stepper Card
        pipe_card = self._create_card(
            p,
            title="即時處理管線 (Processing Pipeline)",
            subtitle="自動追蹤 data/runtime_status.json 工作階段",
            pad=14,
        )
        pipe_card.pack(fill="x", pady=(0, 12))
=======
                justify="left",
            )
            lbl_sub.pack(fill="x")
            self.kpi_cards[key] = {"frame": c, "val": lbl_val, "sub": lbl_sub}

        # Top Section 2: Responsive Vector Pipeline Stepper Card
        pipe_card = self._create_card(
            upper_pane,
            title="即時處理管線 (Processing Pipeline)",
            subtitle="自動追蹤 data/runtime_status.json 工作階段（可拉動下方分隔線調整終端機高度）",
            pad=12,
        )
        pipe_card.pack(fill="x", pady=(0, 6))
>>>>>>> theirs

        self.pipeline_canvas = PipelineStepperCanvas(pipe_card)
        self.pipeline_canvas.pack(fill="x")

        pipe_footer = tk.Frame(pipe_card, bg=PALETTE["bg_card"])
<<<<<<< ours
        pipe_footer.pack(fill="x", pady=(8, 0))
=======
        pipe_footer.pack(fill="x", pady=(6, 0))
>>>>>>> theirs
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

<<<<<<< ours
        # Row 3: Bot Control Action Bar & Console Filter
        ctrl_bar = tk.Frame(p, bg=PALETTE["bg_app"])
        ctrl_bar.pack(fill="x", pady=(0, 10))

        self.btn_start = ModernButton(ctrl_bar, "▶  啟動 Diana", self.action_start_bot, variant="success", padx=16, pady=8)
        self.btn_start.pack(side="left", padx=(0, 8))

        self.btn_stop = ModernButton(ctrl_bar, "■  停止", self.action_stop_bot, variant="danger", padx=14, pady=8)
        self.btn_stop.pack(side="left", padx=(0, 8))

        self.btn_restart = ModernButton(ctrl_bar, "↻  重新啟動", self.action_restart_bot, variant="secondary", padx=14, pady=8)
        self.btn_restart.pack(side="left", padx=(0, 8))

        # Right side log controls
        ModernButton(ctrl_bar, "清除日誌", self.clear_log_console, variant="ghost", padx=10, pady=6).pack(side="right")
        ModernButton(ctrl_bar, "複製日誌", self.copy_log_console, variant="ghost", padx=10, pady=6).pack(side="right", padx=(0, 6))

        chk_auto = tk.Checkbutton(
            ctrl_bar,
=======
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
>>>>>>> theirs
            text="自動捲動",
            variable=self.auto_scroll_var,
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_secondary"],
            selectcolor=PALETTE["bg_input"],
            activebackground=PALETTE["bg_app"],
            activeforeground=PALETTE["text_primary"],
            font=("Microsoft JhengHei UI", 9),
        )
<<<<<<< ours
        chk_auto.pack(side="right", padx=8)

        search_box = tk.Frame(
            ctrl_bar,
=======
        chk_auto.pack(side="right", padx=6)

        search_box = tk.Frame(
            self.ctrl_right_group,
>>>>>>> theirs
            bg=PALETTE["bg_input"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=8,
            pady=4,
        )
<<<<<<< ours
        search_box.pack(side="right", padx=6)
=======
        search_box.pack(side="right", padx=4)
>>>>>>> theirs
        tk.Label(search_box, text="🔍 篩選:", bg=PALETTE["bg_input"], fg=PALETTE["text_muted"], font=("Microsoft JhengHei UI", 9)).pack(side="left")
        filter_entry = tk.Entry(
            search_box,
            textvariable=self.log_filter_var,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            insertbackground="#FFFFFF",
            relief="flat",
<<<<<<< ours
            width=18,
=======
            width=16,
>>>>>>> theirs
            font=("Consolas", 9),
        )
        filter_entry.pack(side="left", padx=4)
        self.log_filter_var.trace_add("write", lambda *_: self._rerender_filtered_logs())

<<<<<<< ours
        # Row 4: Color-Coded Terminal Console
        log_card = self._create_card(p, title="", pad=10)
=======
        # Lower Section 2: Color-Coded Terminal Console (expands to fill remaining height)
        log_card = self._create_card(lower_pane, title="", pad=8)
>>>>>>> theirs
        log_card.pack(fill="both", expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_card,
<<<<<<< ours
            height=14,
=======
            height=10,
>>>>>>> theirs
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

<<<<<<< ours
    # -----------------------------------------------------------------------
    # Page 2: Connections & Independent Credentials
=======
        self.dashboard_paned.add(upper_pane, minsize=210, stretch="never")
        self.dashboard_paned.add(lower_pane, minsize=180, stretch="always")

    # -----------------------------------------------------------------------
    # Page 2: Responsive Connections & Independent Credentials
>>>>>>> theirs
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
<<<<<<< ours

        lbl_col = tk.Frame(row, bg=PALETTE["bg_card"], width=180)
        lbl_col.pack(side="left", fill="y")
        lbl_col.pack_propagate(False)
=======
        row.columnconfigure(1, weight=1)

        lbl_col = tk.Frame(row, bg=PALETTE["bg_card"], width=185)
        lbl_col.grid(row=0, column=0, sticky="nw", padx=(0, 12))
>>>>>>> theirs
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
<<<<<<< ours
        entry_wrap.pack(side="left", fill="x", expand=True)
=======
        entry_wrap.grid(row=0, column=1, sticky="ew")
>>>>>>> theirs

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
<<<<<<< ours
        tab_bar.pack(fill="x", pady=(0, 14))
=======
        tab_bar.pack(fill="x", pady=(0, 10))
>>>>>>> theirs

        self.conn_subpages: Dict[str, tk.Frame] = {}
        self.conn_tab_btns: Dict[str, tk.Label] = {}
        conn_tabs = [
            ("local", "🖥️  本地端 Local API"),
            ("web", "🌐  雲端 Web API"),
            ("image", "🖼️  繪圖 Image API"),
            ("keys", "🔑  獨立憑證與金鑰庫"),
        ]

<<<<<<< ours
        body_wrap = tk.Frame(p, bg=PALETTE["bg_app"])
        body_wrap.pack(fill="both", expand=True)
=======
        scroll_host = ResponsiveScrollFrame(p, bg=PALETTE["bg_app"])
        scroll_host.pack(fill="both", expand=True)
        body_wrap = scroll_host.content
>>>>>>> theirs

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
<<<<<<< ours
=======
            scroll_host.after(20, scroll_host._update_scrollregion_and_bar)
>>>>>>> theirs

        for tab_key, tab_title in conn_tabs:
            btn = tk.Label(
                tab_bar,
                text=tab_title,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_secondary"],
                font=("Microsoft JhengHei UI", 10, "bold"),
<<<<<<< ours
                padx=18,
                pady=8,
=======
                padx=16,
                pady=7,
>>>>>>> theirs
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
            subtitle="適用 LM Studio / Ollama / vLLM 等 OpenAI 相容端點",
        )
        c_local.pack(fill="x")
        self._create_labeled_entry(c_local, "Base URL", self.vars["local_base_url"], "例: http://127.0.0.1:1234/v1")
        self._create_labeled_entry(c_local, "Model Name", self.vars["local_model"], "例: google/gemma-4-e2b")
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

<<<<<<< ours
        # Bottom Save Bar
        save_bar = tk.Frame(p, bg=PALETTE["bg_app"])
        save_bar.pack(fill="x", pady=(12, 0))
        tk.Label(
            save_bar,
            text="💡 提示：修改設定並儲存後，若 Diana 正在運行，重新啟動即可套用最新連線參數。",
=======
        # Sticky Bottom Save Bar
        save_bar = tk.Frame(p, bg=PALETTE["bg_app"])
        save_bar.pack(fill="x", pady=(10, 0))
        tk.Label(
            save_bar,
            text="💡 修改設定並儲存後，若 Diana 正在運行，點選「重新啟動」即可套用最新連線參數。",
>>>>>>> theirs
            bg=PALETTE["bg_app"],
            fg=PALETTE["text_secondary"],
            font=("Microsoft JhengHei UI", 9),
        ).pack(side="left")
<<<<<<< ours
        ModernButton(save_bar, "💾  儲存所有連線與金鑰設定", self.save_all_settings, variant="primary", padx=20, pady=9).pack(side="right")
=======
        ModernButton(save_bar, "💾  儲存所有連線與金鑰設定", self.save_all_settings, variant="primary", padx=20, pady=8).pack(side="right")
>>>>>>> theirs

        _switch_conn_tab("local")

    # -----------------------------------------------------------------------
<<<<<<< ours
    # Page 3: Dual-Mode Persona Studio (Visual Form <-> Raw JSON + AI Gen)
=======
    # Page 3: Dual-Mode Persona Studio (Responsive Visual Form <-> Raw JSON)
>>>>>>> theirs
    # -----------------------------------------------------------------------
    def _build_persona_page(self) -> None:
        p = self.pages["persona"]

        # Top Toolbar: Presets, AI Generation Prompt, Mode Switch, Save
        top_card = self._create_card(p, pad=12)
<<<<<<< ours
        top_card.pack(fill="x", pady=(0, 10))

        btn_row = tk.Frame(top_card, bg=PALETTE["bg_card"])
        btn_row.pack(fill="x", pady=(0, 10))

        ModernButton(btn_row, "📂 讀取目前使用中設定", self.load_current_persona_file, variant="secondary", padx=12, pady=6).pack(side="left", padx=(0, 6))
        ModernButton(btn_row, "👧 載入黛安娜預設範例", self.load_diana_example, variant="secondary", padx=12, pady=6).pack(side="left", padx=6)
        ModernButton(btn_row, "📄 空白角色模板", self.load_blank_persona, variant="secondary", padx=12, pady=6).pack(side="left", padx=6)

        ModernButton(btn_row, "💾 儲存 Persona 設定", self.save_persona, variant="primary", padx=16, pady=6).pack(side="right")

        # Mode Switcher (Visual Form vs Raw JSON)
        self.persona_mode = "visual"
        self.btn_mode_json = ModernButton(btn_row, "{ } JSON 原始碼模式", lambda: self.switch_persona_editor_mode("json"), variant="ghost", padx=12, pady=6)
        self.btn_mode_json.pack(side="right", padx=6)
        self.btn_mode_visual = ModernButton(btn_row, "📝 視覺化表單模式", lambda: self.switch_persona_editor_mode("visual"), variant="secondary", padx=12, pady=6)
        self.btn_mode_visual.pack(side="right", padx=6)
=======
        top_card.pack(fill="x", pady=(0, 8))

        btn_row = tk.Frame(top_card, bg=PALETTE["bg_card"])
        btn_row.pack(fill="x", pady=(0, 8))

        ModernButton(btn_row, "📂 讀取目前使用中設定", self.load_current_persona_file, variant="secondary", padx=11, pady=6).pack(side="left", padx=(0, 5))
        ModernButton(btn_row, "👧 載入黛安娜預設範例", self.load_diana_example, variant="secondary", padx=11, pady=6).pack(side="left", padx=5)
        ModernButton(btn_row, "📄 空白角色模板", self.load_blank_persona, variant="secondary", padx=11, pady=6).pack(side="left", padx=5)

        ModernButton(btn_row, "💾 儲存 Persona 設定", self.save_persona, variant="primary", padx=15, pady=6).pack(side="right")

        # Mode Switcher (Visual Form vs Raw JSON)
        self.persona_mode = "visual"
        self.btn_mode_json = ModernButton(btn_row, "{ } JSON 原始碼模式", lambda: self.switch_persona_editor_mode("json"), variant="ghost", padx=11, pady=6)
        self.btn_mode_json.pack(side="right", padx=5)
        self.btn_mode_visual = ModernButton(btn_row, "📝 視覺化表單模式", lambda: self.switch_persona_editor_mode("visual"), variant="secondary", padx=11, pady=6)
        self.btn_mode_visual.pack(side="right", padx=5)
>>>>>>> theirs

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

<<<<<<< ours
        # 1) Visual Form Editor Frame
        self.persona_visual_frame = tk.Frame(
            self.persona_editor_stack,
            bg=PALETTE["bg_card"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            padx=16,
            pady=12,
        )
=======
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
>>>>>>> theirs
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

<<<<<<< ours
        self.persona_visual_frame.pack(fill="both", expand=True)

    def _build_persona_visual_form(self, parent: tk.Frame) -> None:
        """Builds a clean two-column structured editor for all Persona fields."""
        parent.columnconfigure(0, weight=1, uniform="pcol")
        parent.columnconfigure(1, weight=1, uniform="pcol")
        parent.rowconfigure(1, weight=1)
=======
        self.persona_scroll_host.pack(fill="both", expand=True)

    def _build_persona_visual_form(self, parent: tk.Frame) -> None:
        """Builds an adaptive structured editor that reflows between 2 columns and 1 column."""
        parent.columnconfigure(0, weight=1, uniform="pcol")
        parent.columnconfigure(1, weight=1, uniform="pcol")
>>>>>>> theirs

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
<<<<<<< ours
        left_col = tk.Frame(parent, bg=PALETTE["bg_card"])
        left_col.grid(row=1, column=0, sticky="nsew", padx=(0, 8))

        # Right column: Personality Traits, Speech Patterns, Tone Rules (1 per line)
        right_col = tk.Frame(parent, bg=PALETTE["bg_card"])
        right_col.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
=======
        self.p_left_col = tk.Frame(parent, bg=PALETTE["bg_card"])
        self.p_left_col.grid(row=1, column=0, sticky="nsew", padx=(0, 8))

        # Right column: Personality Traits, Speech Patterns, Tone Rules (1 per line)
        self.p_right_col = tk.Frame(parent, bg=PALETTE["bg_card"])
        self.p_right_col.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
>>>>>>> theirs

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

<<<<<<< ours
        self.p_bg_text = _make_text_box(left_col, "角色背景設定 (background)", 4)
        self.p_rel_text = _make_text_box(left_col, "與主人的關係 (relationship)", 3)
        self.p_guest_text = _make_text_box(left_col, "對待訪客態度 (guest_attitude)", 3)
        self.p_favor_text = _make_text_box(left_col, "好感度三階段反應 (低 / 中 / 高，每行一段)", 4)

        self.p_traits_text = _make_text_box(right_col, "性格特質 (personality_traits — 每行一條)", 5)
        self.p_speech_text = _make_text_box(right_col, "說話習慣 (speech_patterns — 每行一條)", 5)
        self.p_tone_text = _make_text_box(right_col, "語氣與行為規範 (tone_rules — 每行一條)", 5)
=======
        self.p_bg_text = _make_text_box(self.p_left_col, "角色背景設定 (background)", 4)
        self.p_rel_text = _make_text_box(self.p_left_col, "與主人的關係 (relationship)", 3)
        self.p_guest_text = _make_text_box(self.p_left_col, "對待訪客態度 (guest_attitude)", 3)
        self.p_favor_text = _make_text_box(self.p_left_col, "好感度三階段反應 (低 / 中 / 高，每行一段)", 4)

        self.p_traits_text = _make_text_box(self.p_right_col, "性格特質 (personality_traits — 每行一條)", 5)
        self.p_speech_text = _make_text_box(self.p_right_col, "說話習慣 (speech_patterns — 每行一條)", 5)
        self.p_tone_text = _make_text_box(self.p_right_col, "語氣與行為規範 (tone_rules — 每行一條)", 5)
>>>>>>> theirs

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

        fav = obj.get("favorability_reactions", {})
        if not isinstance(fav, dict):
            fav = {}
        fav_lines = [
            f"[低] {fav.get('low', '')}",
            f"[中] {fav.get('medium', '')}",
            f"[高] {fav.get('high', '')}",
        ]
        _set_txt(self.p_favor_text, "\n".join(fav_lines))

        _set_txt(self.p_traits_text, "\n".join(str(x) for x in obj.get("personality_traits", []) if str(x).strip()))
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
            return items or [""]

        base_obj["personality_traits"] = _lines(self.p_traits_text)
        base_obj["speech_patterns"] = _lines(self.p_speech_text)
        base_obj["tone_rules"] = _lines(self.p_tone_text)

        fav_raw = [line.strip() for line in self.p_favor_text.get("1.0", "end").splitlines() if line.strip()]
        fav_dict = {"low": "", "medium": "", "high": ""}
        for idx, key in enumerate(("low", "medium", "high")):
            if idx < len(fav_raw):
                val = fav_raw[idx]
                for prefix in ("[低]", "[中]", "[高]"):
                    if val.startswith(prefix):
                        val = val[len(prefix):].strip()
                fav_dict[key] = val
        base_obj["favorability_reactions"] = fav_dict
        return base_obj

    def switch_persona_editor_mode(self, target_mode: str) -> None:
        if target_mode == self.persona_mode:
            return
        if target_mode == "json":
<<<<<<< ours
            # Sync Visual Form -> JSON text
            merged = self._collect_visual_form_to_dict()
            self.persona_text.delete("1.0", "end")
            self.persona_text.insert("1.0", json.dumps(merged, ensure_ascii=False, indent=2))
            self.persona_visual_frame.pack_forget()
            self.persona_json_frame.pack(fill="both", expand=True)
        else:
            # Sync JSON text -> Visual Form
=======
            merged = self._collect_visual_form_to_dict()
            self.persona_text.delete("1.0", "end")
            self.persona_text.insert("1.0", json.dumps(merged, ensure_ascii=False, indent=2))
            self.persona_scroll_host.pack_forget()
            self.persona_json_frame.pack(fill="both", expand=True)
        else:
>>>>>>> theirs
            try:
                parsed = json.loads(self.persona_text.get("1.0", "end"))
                self._sync_json_to_visual_form(parsed)
            except Exception as e:
                messagebox.showerror("JSON 語法錯誤", f"請先修正 JSON 格式再切換至表單模式：\n{e}")
                return
            self.persona_json_frame.pack_forget()
<<<<<<< ours
            self.persona_visual_frame.pack(fill="both", expand=True)
=======
            self.persona_scroll_host.pack(fill="both", expand=True)
>>>>>>> theirs
        self.persona_mode = target_mode

    def _set_persona_object(self, obj: Dict[str, Any]) -> None:
        self.persona_text.delete("1.0", "end")
        self.persona_text.insert("1.0", json.dumps(obj, ensure_ascii=False, indent=2))
        self._sync_json_to_visual_form(obj)

    # -----------------------------------------------------------------------
<<<<<<< ours
    # Page 4: System Diagnostics, Parameters & Evolution Control
    # -----------------------------------------------------------------------
    def _build_system_page(self) -> None:
        p = self.pages["system"]

        # Card 1: Environment & File Health Checklist
        diag_card = self._create_card(
            p,
            title="系統必要檔案與環境健檢 (System Health Check)",
            subtitle="自動驗證設定檔、獨立金鑰與參考圖資源狀態",
=======
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
            subtitle="自動驗證設定檔、獨立金鑰與參考圖資源狀態（支援視窗寬度自適應排版）",
>>>>>>> theirs
        )
        diag_card.pack(fill="x", pady=(0, 12))

        self.diag_grid = tk.Frame(diag_card, bg=PALETTE["bg_card"])
        self.diag_grid.pack(fill="x", pady=(0, 10))
<<<<<<< ours
        for col in range(2):
            self.diag_grid.columnconfigure(col, weight=1, uniform="diag")
=======
        self.diag_cells: List[tk.Frame] = []
>>>>>>> theirs

        diag_btn_row = tk.Frame(diag_card, bg=PALETTE["bg_card"])
        diag_btn_row.pack(fill="x")

        self.btn_install_deps = ModernButton(
            diag_btn_row,
            "📦 安裝 / 更新 requirements.txt",
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
<<<<<<< ours
            p,
=======
            body,
>>>>>>> theirs
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

    def refresh_file_diagnostics(self) -> None:
        for child in self.diag_grid.winfo_children():
            child.destroy()
<<<<<<< ours
=======
        self.diag_cells.clear()
>>>>>>> theirs

        ref_count = len(list(REFERENCE_IMG_DIR.glob("*.*"))) if REFERENCE_IMG_DIR.exists() else 0
        checks = [
            ("核心主程式 (bot.py)", (ROOT / "bot.py").exists(), "bot.py"),
            ("演化引擎 (diana_evolution.py)", (ROOT / "diana_evolution.py").exists(), "diana_evolution.py"),
            ("角色設定檔 (character_persona.json)", PERSONA.exists(), "config/character_persona.json"),
            ("Discord Bot Token", bool(ConfigRepository.read_first_line(TOKEN)), "config/discord_token.txt"),
            ("Chat API Key (聊天專用)", bool(ConfigRepository.read_first_line(CHAT_KEY)), "config/google_api_key_chat.txt"),
            ("Evolution API Key (演化專用)", bool(ConfigRepository.read_first_line(EVOLUTION_KEY)), "config/google_api_key_evolution.txt"),
            ("Image API Key (生圖專用)", bool(ConfigRepository.read_first_line(IMAGE_KEY)), "config/google_api_key_image.txt"),
            (f"角色參考圖庫 ({ref_count} 張)", ref_count > 0, "img/reference/"),
        ]

<<<<<<< ours
        for idx, (label, ok, path_hint) in enumerate(checks):
            r, c = divmod(idx, 2)
=======
        d_cols = 2 if self.viewport.dual_col_forms else 1
        for c in range(2):
            self.diag_grid.columnconfigure(c, weight=1 if c < d_cols else 0, uniform="diag" if c < d_cols else "")

        for idx, (label, ok, path_hint) in enumerate(checks):
            r, c = divmod(idx, d_cols)
>>>>>>> theirs
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
            badge_txt = "✓ 已就緒" if ok else "⚠ 待設定"
            tk.Label(cell, text=badge_txt, bg=PALETTE["bg_input"], fg=badge_fg, font=("Microsoft JhengHei UI", 9, "bold")).pack(side="left", padx=(0, 10))
            tk.Label(cell, text=label, bg=PALETTE["bg_input"], fg=PALETTE["text_primary"], font=("Microsoft JhengHei UI", 9, "bold")).pack(side="left")
            tk.Label(cell, text=path_hint, bg=PALETTE["bg_input"], fg=PALETTE["text_muted"], font=("Consolas", 8)).pack(side="right")
<<<<<<< ours
=======
            self.diag_cells.append(cell)
>>>>>>> theirs

    # -----------------------------------------------------------------------
    # Settings & Persona Actions (Orchestration Layer)
    # -----------------------------------------------------------------------
    def load_all_settings(self) -> None:
        self.cfg = ConfigRepository.load_gui_config()
        for k in ["local_base_url", "local_model", "web_base_url", "web_model", "image_endpoint", "image_model"]:
            self.vars[k].set(str(self.cfg.get(k, DEFAULT_CFG[k])))

        self.vars["discord"].set(ConfigRepository.read_first_line(TOKEN))
        self.vars["chat_key"].set(ConfigRepository.read_first_line(CHAT_KEY))
        self.vars["evolution_key"].set(ConfigRepository.read_first_line(EVOLUTION_KEY))
        self.vars["evolution_model"].set(ConfigRepository.read_second_line(EVOLUTION_KEY))
        self.vars["image_key"].set(ConfigRepository.read_first_line(IMAGE_KEY))
        self.vars["temperature"].set(str(self.cfg.get("temperature", 0.75)))
        self.vars["thinking_tokens"].set(str(self.cfg.get("thinking_tokens", 256)))

        self.load_current_persona_file(silent=True)
        self.refresh_file_diagnostics()
        self._refresh_dashboard_telemetry()

    def save_all_settings(self, silent: bool = False) -> bool:
        try:
            for k in ["local_base_url", "local_model", "web_base_url", "web_model", "image_endpoint", "image_model"]:
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

    def save_persona(self) -> None:
        try:
            if self.persona_mode == "visual":
                obj = self._collect_visual_form_to_dict()
                self.persona_text.delete("1.0", "end")
                self.persona_text.insert("1.0", json.dumps(obj, ensure_ascii=False, indent=2))
            else:
                obj = json.loads(self.persona_text.get("1.0", "end"))
                self._sync_json_to_visual_form(obj)

            ConfigRepository.write_json(PERSONA, obj)
            self._refresh_dashboard_telemetry()
            self.show_toast(f"✓ 角色「{obj.get('name', '未命名')}」已儲存至 config/character_persona.json", level="success")
        except Exception as e:
            messagebox.showerror("Persona 儲存失敗", f"JSON 格式無效或寫入失敗：\n{e}")

    # -----------------------------------------------------------------------
    # Worker Trigger Actions
    # -----------------------------------------------------------------------
<<<<<<< ours
=======
    def action_open_patch_pusher(self) -> None:
        if self.workers.launch_patch_pusher():
            self.show_toast("🩹 已開啟 Diana Patch Pusher 補丁推送工具", level="info")
        else:
            self.show_toast("⚠ 找不到 patch_pusher.py", level="warn")

>>>>>>> theirs
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
<<<<<<< ours
        win.geometry("940x640")
=======
        win.geometry("920x620")
        win.minsize(720, 480)
        win.resizable(True, True)
>>>>>>> theirs
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
                    self.show_toast("📦 正在執行 pip install -r requirements.txt...", level="info")

                elif stype == "DEPS_INSTALL_FINISHED":
                    self.btn_install_deps.set_enabled(True, "📦 安裝 / 更新 requirements.txt")
                    rc = p.get("returncode", -1)
                    if rc == 0:
                        self.show_toast("✓ requirements.txt 依賴套件安裝完成", level="success")
                    else:
                        self.show_toast(f"⚠ 套件安裝結束 (代碼 {rc})，請查看日誌", level="warn")

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
<<<<<<< ours
        self.kpi_cards["status"]["sub"].configure(text=detail[:28])
=======
        self.kpi_cards["status"]["sub"].configure(text=detail[:36])
>>>>>>> theirs

        # Update KPI Card 2: Persona & Version
        self.kpi_cards["persona"]["val"].configure(text=snap["persona_name"])
        self.kpi_cards["persona"]["sub"].configure(
            text=f"Bot v{snap['bot_version']} · Persona v{snap['persona_version']}"
        )

        # Update KPI Card 3: Social & Memory
        self.kpi_cards["social"]["val"].configure(text=f"{snap['user_count']} 位互動對象")
        self.kpi_cards["social"]["sub"].configure(
            text=f"最高好感 {snap['max_favor']} · 提醒 {snap['reminder_count']} 筆 · 插圖 {snap['action_img_count']}"
        )

        # Update KPI Card 4: LLM Engine
<<<<<<< ours
        loc_m = self.vars["local_model"].get().split("/")[-1][:16]
=======
        loc_m = self.vars["local_model"].get().split("/")[-1][:18]
>>>>>>> theirs
        self.kpi_cards["engine"]["val"].configure(text=loc_m or "Local + Web")
        self.kpi_cards["engine"]["sub"].configure(
            text=f"Temp {self.vars['temperature'].get()} · Think {self.vars['thinking_tokens'].get()}"
        )

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

=======

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
    workspace_width: int = 985
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

        sidebar_w = 68 if collapsed else 232
        ws_w = max(win_w - sidebar_w, 360)

        kpi_cols = 2 if ws_w < cls.COMPACT_WORKSPACE_W else 4
        dual_forms = ws_w >= cls.COMPACT_WORKSPACE_W
        compact_tb = ws_w < 860

        changed = (
            bp != current.breakpoint
            or collapsed != current.sidebar_collapsed
            or kpi_cols != current.kpi_columns
            or dual_forms != current.dual_col_forms
            or compact_tb != current.compact_toolbar
            or abs(ws_w - current.workspace_width) >= 24
        )

        new_state = ViewportLayoutState(
            window_width=win_w,
            window_height=win_h,
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

    @staticmethod
    def blank_persona_template() -> Dict[str, Any]:
        return {
            "name": "",
            "age_style": "",
            "background": "",
            "relationship": "",
            "guest_attitude": "",
            "personality_traits": [""],
            "speech_patterns": [""],
            "tone_rules": [""],
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

        return {
            "bot_version": version_data.get("bot", "—"),
            "persona_version": version_data.get("character_persona", "—"),
            "persona_name": persona_data.get("name", "未設定") if isinstance(persona_data, dict) else "未設定",
            "persona_age_style": persona_data.get("age_style", "") if isinstance(persona_data, dict) else "",
            "user_count": user_count,
            "max_favor": max_favor,
            "reminder_count": reminder_count,
            "action_img_count": action_img_count,
            "last_message_time": bot_state.get("last_message_time", "無紀錄"),
            "runtime": runtime_status,
        }


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
        if not PATCH_PUSHER_SCRIPT.exists():
            return False
        exe_dir = Path(sys.executable).resolve().parent
        pythonw = exe_dir / "pythonw.exe"
        runner = str(pythonw) if (sys.platform.startswith("win") and pythonw.exists()) else sys.executable
        subprocess.Popen([runner, str(PATCH_PUSHER_SCRIPT)], cwd=str(ROOT))
        return True

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
                    text=f"\n[{datetime.now().strftime('%H:%M:%S')}] === 📦 開始安裝 requirements.txt 依賴套件 ===\n",
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
                self.emit("DEPS_INSTALL_FINISHED", returncode=proc.returncode)
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

            # 1. Reflow Collapsible Sidebar Rail (68px icon mode vs 232px full mode)
            target_sb_w = 68 if vp.sidebar_collapsed else 232
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
                    if not widgets["desc"].winfo_ismapped():
                        widgets["desc"].pack(fill="x", pady=(2, 0))
                    widgets["inner"].configure(padx=12, pady=9)

            # 2. Reflow Dashboard 4 KPI Cards (1x4 wide vs 2x2 compact)
            cols = vp.kpi_columns
            for c in range(4):
                self.kpi_row.columnconfigure(c, weight=1 if c < cols else 0, uniform="kpi" if c < cols else "")

            card_wrap_px = max((vp.workspace_width - 64) // max(cols, 1) - 28, 130)
            for idx, key in enumerate(("status", "persona", "social", "engine")):
                card_info = self.kpi_cards[key]
                frame = card_info["frame"]
                r = idx // cols
                c = idx % cols
                pad_l = 0 if c == 0 else 5
                pad_r = 0 if c == (cols - 1) else 5
                pad_b = 6 if (cols == 2 and r == 0) else 0
                frame.grid_configure(row=r, column=c, sticky="nsew", padx=(pad_l, pad_r), pady=(0, pad_b))
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
        # Left Collapsible Sidebar Navigation Rail
        self.sidebar = tk.Frame(
            self,
            bg=PALETTE["bg_sidebar"],
            width=232,
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

        if PATCH_PUSHER_SCRIPT.exists():
            ModernButton(
                self.topbar,
                "🩹 開啟補丁推送工具",
                self.action_open_patch_pusher,
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
            for pid in ("dashboard", "connections", "persona", "system")
        }

        self._build_dashboard_page()
        self._build_connections_page()
        self._build_persona_page()
        self._build_system_page()
        self.switch_page("dashboard")

    def switch_page(self, page_id: str) -> None:
        self.lifecycle.active_page = page_id
        titles = {
            "dashboard": "即時儀表板 (Runtime Dashboard)",
            "connections": "連線與獨立憑證設定 (API & Credentials)",
            "persona": "角色設定工坊 (Persona Studio)",
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
        kpi_defs = [
            ("status", "執行狀態 (RUNTIME)", "OFFLINE", "尚未啟動 bot.py"),
            ("persona", "目前角色 (PERSONA)", "黛安娜", "Bot v— · Persona v—"),
            ("social", "關係與排程 (MEMORY)", "0 位互動對象", "最高好感 0 · 提醒 0 筆"),
            ("engine", "推理引擎 (LLM ENGINE)", "Local + Web", "Temp 0.75 · Think 256"),
        ]
        for idx, (key, title, val, sub) in enumerate(kpi_defs):
            c = tk.Frame(
                self.kpi_row,
                bg=PALETTE["bg_card"],
                highlightthickness=1,
                highlightbackground=PALETTE["border"],
                padx=14,
                pady=10,
            )
            c.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 5, 0 if idx == 3 else 5))
            tk.Label(
                c,
                text=title,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_muted"],
                font=("Segoe UI", 8, "bold"),
                anchor="w",
            ).pack(fill="x")
            lbl_val = tk.Label(
                c,
                text=val,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_primary"],
                font=("Microsoft JhengHei UI", 13, "bold"),
                anchor="w",
            )
            lbl_val.pack(fill="x", pady=(3, 1))
            lbl_sub = tk.Label(
                c,
                text=sub,
                bg=PALETTE["bg_card"],
                fg=PALETTE["text_secondary"],
                font=("Microsoft JhengHei UI", 9),
                anchor="w",
                justify="left",
            )
            lbl_sub.pack(fill="x")
            self.kpi_cards[key] = {"frame": c, "val": lbl_val, "sub": lbl_sub}

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
            subtitle="適用 LM Studio / Ollama / vLLM 等 OpenAI 相容端點",
        )
        c_local.pack(fill="x")
        self._create_labeled_entry(c_local, "Base URL", self.vars["local_base_url"], "例: http://127.0.0.1:1234/v1")
        self._create_labeled_entry(c_local, "Model Name", self.vars["local_model"], "例: google/gemma-4-e2b")
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

        ModernButton(btn_row, "💾 儲存 Persona 設定", self.save_persona, variant="primary", padx=15, pady=6).pack(side="right")

        # Mode Switcher (Visual Form vs Raw JSON)
        self.persona_mode = "visual"
        self.btn_mode_json = ModernButton(btn_row, "{ } JSON 原始碼模式", lambda: self.switch_persona_editor_mode("json"), variant="ghost", padx=11, pady=6)
        self.btn_mode_json.pack(side="right", padx=5)
        self.btn_mode_visual = ModernButton(btn_row, "📝 視覺化表單模式", lambda: self.switch_persona_editor_mode("visual"), variant="secondary", padx=11, pady=6)
        self.btn_mode_visual.pack(side="right", padx=5)

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

        self.p_bg_text = _make_text_box(self.p_left_col, "角色背景設定 (background)", 4)
        self.p_rel_text = _make_text_box(self.p_left_col, "與主人的關係 (relationship)", 3)
        self.p_guest_text = _make_text_box(self.p_left_col, "對待訪客態度 (guest_attitude)", 3)
        self.p_favor_text = _make_text_box(self.p_left_col, "好感度三階段反應 (低 / 中 / 高，每行一段)", 4)

        self.p_traits_text = _make_text_box(self.p_right_col, "性格特質 (personality_traits — 每行一條)", 5)
        self.p_speech_text = _make_text_box(self.p_right_col, "說話習慣 (speech_patterns — 每行一條)", 5)
        self.p_tone_text = _make_text_box(self.p_right_col, "語氣與行為規範 (tone_rules — 每行一條)", 5)

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

        fav = obj.get("favorability_reactions", {})
        if not isinstance(fav, dict):
            fav = {}
        fav_lines = [
            f"[低] {fav.get('low', '')}",
            f"[中] {fav.get('medium', '')}",
            f"[高] {fav.get('high', '')}",
        ]
        _set_txt(self.p_favor_text, "\n".join(fav_lines))

        _set_txt(self.p_traits_text, "\n".join(str(x) for x in obj.get("personality_traits", []) if str(x).strip()))
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
            return items or [""]

        base_obj["personality_traits"] = _lines(self.p_traits_text)
        base_obj["speech_patterns"] = _lines(self.p_speech_text)
        base_obj["tone_rules"] = _lines(self.p_tone_text)

        fav_raw = [line.strip() for line in self.p_favor_text.get("1.0", "end").splitlines() if line.strip()]
        fav_dict = {"low": "", "medium": "", "high": ""}
        for idx, key in enumerate(("low", "medium", "high")):
            if idx < len(fav_raw):
                val = fav_raw[idx]
                for prefix in ("[低]", "[中]", "[高]"):
                    if val.startswith(prefix):
                        val = val[len(prefix):].strip()
                fav_dict[key] = val
        base_obj["favorability_reactions"] = fav_dict
        return base_obj

    def switch_persona_editor_mode(self, target_mode: str) -> None:
        if target_mode == self.persona_mode:
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
                return
            self.persona_json_frame.pack_forget()
            self.persona_scroll_host.pack(fill="both", expand=True)
        self.persona_mode = target_mode

    def _set_persona_object(self, obj: Dict[str, Any]) -> None:
        self.persona_text.delete("1.0", "end")
        self.persona_text.insert("1.0", json.dumps(obj, ensure_ascii=False, indent=2))
        self._sync_json_to_visual_form(obj)

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
            subtitle="自動驗證設定檔、獨立金鑰與參考圖資源狀態（支援視窗寬度自適應排版）",
        )
        diag_card.pack(fill="x", pady=(0, 12))

        self.diag_grid = tk.Frame(diag_card, bg=PALETTE["bg_card"])
        self.diag_grid.pack(fill="x", pady=(0, 10))
        self.diag_cells: List[tk.Frame] = []

        diag_btn_row = tk.Frame(diag_card, bg=PALETTE["bg_card"])
        diag_btn_row.pack(fill="x")

        self.btn_install_deps = ModernButton(
            diag_btn_row,
            "📦 安裝 / 更新 requirements.txt",
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

    def refresh_file_diagnostics(self) -> None:
        for child in self.diag_grid.winfo_children():
            child.destroy()
        self.diag_cells.clear()

        ref_count = len(list(REFERENCE_IMG_DIR.glob("*.*"))) if REFERENCE_IMG_DIR.exists() else 0
        checks = [
            ("核心主程式 (bot.py)", (ROOT / "bot.py").exists(), "bot.py"),
            ("演化引擎 (diana_evolution.py)", (ROOT / "diana_evolution.py").exists(), "diana_evolution.py"),
            ("角色設定檔 (character_persona.json)", PERSONA.exists(), "config/character_persona.json"),
            ("Discord Bot Token", bool(ConfigRepository.read_first_line(TOKEN)), "config/discord_token.txt"),
            ("Chat API Key (聊天專用)", bool(ConfigRepository.read_first_line(CHAT_KEY)), "config/google_api_key_chat.txt"),
            ("Evolution API Key (演化專用)", bool(ConfigRepository.read_first_line(EVOLUTION_KEY)), "config/google_api_key_evolution.txt"),
            ("Image API Key (生圖專用)", bool(ConfigRepository.read_first_line(IMAGE_KEY)), "config/google_api_key_image.txt"),
            (f"角色參考圖庫 ({ref_count} 張)", ref_count > 0, "img/reference/"),
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
            badge_txt = "✓ 已就緒" if ok else "⚠ 待設定"
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

        self.vars["discord"].set(ConfigRepository.read_first_line(TOKEN))
        self.vars["chat_key"].set(ConfigRepository.read_first_line(CHAT_KEY))
        self.vars["evolution_key"].set(ConfigRepository.read_first_line(EVOLUTION_KEY))
        self.vars["evolution_model"].set(ConfigRepository.read_second_line(EVOLUTION_KEY))
        self.vars["image_key"].set(ConfigRepository.read_first_line(IMAGE_KEY))
        self.vars["temperature"].set(str(self.cfg.get("temperature", 0.75)))
        self.vars["thinking_tokens"].set(str(self.cfg.get("thinking_tokens", 256)))

        self.load_current_persona_file(silent=True)
        self.refresh_file_diagnostics()
        self._refresh_dashboard_telemetry()

    def save_all_settings(self, silent: bool = False) -> bool:
        try:
            for k in ["local_base_url", "local_model", "web_base_url", "web_model", "image_endpoint", "image_model"]:
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

    def save_persona(self) -> None:
        try:
            if self.persona_mode == "visual":
                obj = self._collect_visual_form_to_dict()
                self.persona_text.delete("1.0", "end")
                self.persona_text.insert("1.0", json.dumps(obj, ensure_ascii=False, indent=2))
            else:
                obj = json.loads(self.persona_text.get("1.0", "end"))
                self._sync_json_to_visual_form(obj)

            ConfigRepository.write_json(PERSONA, obj)
            self._refresh_dashboard_telemetry()
            self.show_toast(f"✓ 角色「{obj.get('name', '未命名')}」已儲存至 config/character_persona.json", level="success")
        except Exception as e:
            messagebox.showerror("Persona 儲存失敗", f"JSON 格式無效或寫入失敗：\n{e}")

    # -----------------------------------------------------------------------
    # Worker Trigger Actions
    # -----------------------------------------------------------------------
    def action_open_patch_pusher(self) -> None:
        if self.workers.launch_patch_pusher():
            self.show_toast("🩹 已開啟 Diana Patch Pusher 補丁推送工具", level="info")
        else:
            self.show_toast("⚠ 找不到 patch_pusher.py", level="warn")

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
                    self.show_toast("📦 正在執行 pip install -r requirements.txt...", level="info")

                elif stype == "DEPS_INSTALL_FINISHED":
                    self.btn_install_deps.set_enabled(True, "📦 安裝 / 更新 requirements.txt")
                    rc = p.get("returncode", -1)
                    if rc == 0:
                        self.show_toast("✓ requirements.txt 依賴套件安裝完成", level="success")
                    else:
                        self.show_toast(f"⚠ 套件安裝結束 (代碼 {rc})，請查看日誌", level="warn")

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

        # Update KPI Card 2: Persona & Version
        self.kpi_cards["persona"]["val"].configure(text=snap["persona_name"])
        self.kpi_cards["persona"]["sub"].configure(
            text=f"Bot v{snap['bot_version']} · Persona v{snap['persona_version']}"
        )

        # Update KPI Card 3: Social & Memory
        self.kpi_cards["social"]["val"].configure(text=f"{snap['user_count']} 位互動對象")
        self.kpi_cards["social"]["sub"].configure(
            text=f"最高好感 {snap['max_favor']} · 提醒 {snap['reminder_count']} 筆 · 插圖 {snap['action_img_count']}"
        )

        # Update KPI Card 4: LLM Engine
        loc_m = self.vars["local_model"].get().split("/")[-1][:18]
        self.kpi_cards["engine"]["val"].configure(text=loc_m or "Local + Web")
        self.kpi_cards["engine"]["sub"].configure(
            text=f"Temp {self.vars['temperature'].get()} · Think {self.vars['thinking_tokens'].get()}"
        )

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

>>>>>>> theirs

# Backwards-compatible alias
App = DianaControlCenterApp

if __name__ == "__main__":
    DianaControlCenterApp().mainloop()
