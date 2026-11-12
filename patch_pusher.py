#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Patch Pusher — 把 .patch 拖進專案資料夾，一鍵安裝並推送到 GitHub (SSH)
=====================================================================

使用方式
    python patch_pusher.py [補丁檔]        開啟安裝視窗（可帶入 .patch / .diff）
    python patch_pusher.py --install       安裝為應用程式（Windows）
    python patch_pusher.py --uninstall     解除安裝（Windows）
    python patch_pusher.py --export-icon X 匯出 .ico（給 PyInstaller 用）

流程（拖曳確認後自動執行）
    檢查環境 → 驗證 GitHub 連線 → 同步遠端 → 檢查本地變更 → 切換分支
    → 套用補丁 → 提交 → 推送

安全設計
    * 絕不 reset / checkout 覆蓋你未提交的修改；有衝突時會詢問是否自動 stash。
    * 補丁套用失敗會自動還原分支，不會留下半套用的狀態。
    * 不修改專案的 user.name / user.email（改用 git -c 單次覆寫）。
    * SSH 一律 BatchMode，避免無視窗時卡在密碼 / host key 提示。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import dataclasses
import email.header
import email.parser
import hashlib
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import zlib
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from tkinter import font as tkfont
from typing import Any, Callable, Dict, List, Optional, Tuple

APP_NAME = "Patch Pusher"
APP_VERSION = "2.0.0"
APP_ID = "PatchPusher"
IS_WIN = sys.platform.startswith("win")
FROZEN = bool(getattr(sys, "frozen", False))
SELF_PATH = Path(sys.executable if FROZEN else __file__).resolve()
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0

C = {
    "bg": "#0E1320",
    "panel": "#161D2E",
    "panel2": "#202942",
    "border": "#2B3555",
    "accent": "#6366F1",
    "accent_hi": "#A5B4FC",
    "green": "#34D399",
    "green_dim": "#0B3B2E",
    "amber": "#FBBF24",
    "red": "#F87171",
    "text": "#F1F5F9",
    "sub": "#B6C0D4",
    "muted": "#6B7790",
}


# ---------------------------------------------------------------------------
# 1. 設定檔
# ---------------------------------------------------------------------------
def config_dir() -> Path:
    if IS_WIN:
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP_ID


CONFIG_FILE = config_dir() / "config.json"

DEFAULT_CFG: Dict[str, Any] = {
    "repo_path": "",        # 補丁要安裝到的本地專案資料夾
    "ssh_url": "",          # git@github.com:user/repo.git
    "branch": "",           # 推送目標分支
    "remote": "origin",
    "user_name": "",        # 留空 = 使用 Git 現有設定
    "user_email": "",
    "ssh_key": "",          # 留空 = 使用系統預設 / ssh-agent
    "fallback_msg": "chore: apply patch",
    "archive": False,       # 成功後把 .patch 移到 done_patches/
    "auto_close": False,    # 成功後自動關閉視窗
}

# 舊版 (Diana Patch Pusher) 欄位 → 新欄位
LEGACY_MAP = {
    "local_repo_path": "repo_path", "github_ssh_url": "ssh_url", "remote_url": "ssh_url",
    "target_branch": "branch", "remote_name": "remote", "git_user_name": "user_name",
    "git_user_email": "user_email", "ssh_key_path": "ssh_key",
    "default_commit_msg": "fallback_msg", "archive_after_push": "archive",
}


def _read_json(p: Path) -> Optional[dict]:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def normalize_key(p: str) -> str:
    p = (p or "").strip()
    return p[:-4] if p.lower().endswith(".pub") else p


def load_config() -> Tuple[Dict[str, Any], bool]:
    """回傳 (設定, 是否已有設定)。第一次執行會嘗試匯入舊版設定。"""
    cfg = dict(DEFAULT_CFG)
    raw: Optional[dict] = None
    existed = CONFIG_FILE.exists()
    if existed:
        raw = _read_json(CONFIG_FILE)
    else:
        for legacy in (SELF_PATH.parent / "config" / "patch_pusher_config.json",
                       Path.cwd() / "config" / "patch_pusher_config.json"):
            old = _read_json(legacy)
            if old:
                raw = {LEGACY_MAP.get(k, k): v for k, v in old.items()}
                existed = True
                break
    if raw:
        for k, default in DEFAULT_CFG.items():
            if k in raw and isinstance(raw[k], type(default)):
                cfg[k] = raw[k]
    cfg["ssh_key"] = normalize_key(cfg["ssh_key"])
    if existed and not CONFIG_FILE.exists():
        save_config(cfg)
    return cfg, existed


def save_config(cfg: Dict[str, Any]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg = dict(cfg)
    cfg["ssh_key"] = normalize_key(cfg.get("ssh_key", ""))
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


def missing_settings(cfg: Dict[str, Any]) -> List[str]:
    out = []
    rp = (cfg.get("repo_path") or "").strip()
    if not rp or not Path(rp).expanduser().is_dir():
        out.append("專案資料夾")
    if not (cfg.get("ssh_url") or "").strip():
        out.append("GitHub SSH 位址")
    if not (cfg.get("branch") or "").strip():
        out.append("分支")
    return out


# ---------------------------------------------------------------------------
# 2. Git 輔助
# ---------------------------------------------------------------------------
class UserError(Exception):
    """可直接顯示給使用者的錯誤。"""


def find_git() -> Optional[str]:
    g = shutil.which("git")
    if g:
        return g
    if IS_WIN:
        for p in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe",
                  str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe")):
            if Path(p).exists():
                return p
    return None


def build_git_env(ssh_key: str) -> Dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    parts = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=20"]
    key = normalize_key(ssh_key)
    if key:
        kp = str(Path(key).expanduser()).replace("\\", "/")
        parts += ["-i", f'"{kp}"', "-o", "IdentitiesOnly=yes"]
    env["GIT_SSH_COMMAND"] = " ".join(parts)
    return env


def quick_git(args: List[str], cwd: Optional[Path] = None, timeout: int = 8,
              env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    git = find_git()
    if not git:
        return 127, "找不到 git"
    try:
        p = subprocess.run([git] + args, cwd=str(cwd) if cwd else None, env=env,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return p.returncode, p.stdout.strip()
    except Exception as e:
        return 1, str(e)


def explain_git_failure(out: str) -> str:
    o = out.lower()
    rules = [
        ("permission denied (publickey)", "GitHub 拒絕了 SSH 金鑰：請確認設定中的私鑰正確，且公鑰已加入 GitHub（Settings → SSH keys）。"),
        ("host key verification failed", "SSH 主機金鑰驗證失敗：請先在終端機用 ssh -T git@github.com 確認一次。"),
        ("could not resolve hostname", "無法解析 GitHub 主機名稱，請檢查網路連線。"),
        ("repository not found", "找不到遠端專案，或此金鑰沒有存取權限，請檢查 SSH 位址。"),
        ("non-fast-forward", "遠端分支有本地沒有的新提交，推送被拒絕。請先同步（git pull --rebase）後再試。"),
        ("fetch first", "遠端分支有本地沒有的新提交，推送被拒絕。請先同步（git pull --rebase）後再試。"),
        ("protected branch", "目標分支受保護，無法直接推送；請改推到其他分支。"),
        ("please tell me who you are", "尚未設定 Git 名稱 / 信箱，請到「設定」填寫。"),
        ("unable to auto-detect email", "尚未設定 Git 名稱 / 信箱，請到「設定」填寫。"),
        ("timed out", "連線逾時，請檢查網路或防火牆。"),
    ]
    for key, msg in rules:
        if key in o:
            return msg
    return ""


def github_commit_url(ssh_url: str, sha: str) -> str:
    u = (ssh_url or "").strip()
    m = (re.match(r"^(?:ssh://)?git@github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", u)
         or re.match(r"^https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", u))
    return f"https://github.com/{m.group(1)}/{m.group(2)}/commit/{sha}" if (m and sha) else ""


# ---------------------------------------------------------------------------
# 3. 補丁解析
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class PatchInfo:
    path: Path
    subject: str = ""
    files: List[str] = dataclasses.field(default_factory=list)
    touched: List[str] = dataclasses.field(default_factory=list)
    added: int = 0
    removed: int = 0
    commits: int = 0
    is_mbox: bool = False
    error: str = ""


_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def read_patch_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    return data.decode("utf-8", errors="replace")


def prepare_patch_file(path: Path) -> Tuple[Path, Optional[Path]]:
    """PowerShell 的 `>` 常產生 UTF-16/BOM 檔，git 讀不了 → 轉成 UTF-8 暫存檔。"""
    head = path.read_bytes()[:3]
    if head.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
        tmp_dir = Path(tempfile.mkdtemp(prefix="patchpusher_"))
        out = tmp_dir / path.name
        out.write_bytes(read_patch_text(path).encode("utf-8"))
        return out, tmp_dir
    return path, None


def _split_git_header(rest: str) -> Tuple[str, str]:
    rest = rest.strip()
    if rest.startswith('"'):
        m = re.match(r'^"?a/(.*?)"? "?b/(.*?)"?$', rest)
        return (m.group(1), m.group(2)) if m else (rest, rest)
    n = len(rest)
    if n >= 5 and (n - 5) % 2 == 0:
        L = (n - 5) // 2
        if rest[2:2 + L] == rest[n - L:] and rest[2 + L:5 + L] == " b/":
            return rest[2:2 + L], rest[n - L:]
    m = re.match(r"^a/(.+?) b/(.+)$", rest)
    return (m.group(1), m.group(2)) if m else (rest, rest)


def inspect_patch(path: Path) -> PatchInfo:
    info = PatchInfo(path)
    try:
        text = read_patch_text(path)
    except Exception as e:
        info.error = f"無法讀取：{e}"
        return info
    lines = [l.rstrip("\r") for l in text.split("\n")]

    start = 1 if lines and lines[0].startswith("From ") else 0
    hdr: List[str] = []
    for l in lines[start:start + 80]:
        if l == "":
            break
        hdr.append(l)
    has_from = any(h.lower().startswith("from:") for h in hdr)
    has_sub = any(h.lower().startswith("subject:") for h in hdr)
    info.is_mbox = start == 1 or (has_from and has_sub)
    if has_sub:
        try:
            raw = email.parser.HeaderParser().parsestr("\n".join(hdr)).get("Subject", "")
            subj = str(email.header.make_header(email.header.decode_header(str(raw))))
        except Exception:
            subj = next((h.split(":", 1)[1] for h in hdr if h.lower().startswith("subject:")), "")
        subj = re.sub(r"\s+", " ", subj).strip()
        info.subject = re.sub(r"^\[[^\]]*\]\s*", "", subj)

    touched: List[str] = []
    i, n = 0, len(lines)
    while i < n:
        l = lines[i]
        if l.startswith("diff --git "):
            a, b = _split_git_header(l[11:])
            if b not in info.files:
                info.files.append(b)
            for p in (a, b):
                if p not in touched:
                    touched.append(p)
        elif l.startswith(("rename from ", "copy from ")):
            p = l.split(" from ", 1)[1]
            if p not in touched:
                touched.append(p)
        elif re.match(r"^From [0-9a-f]{40} ", l):
            info.commits += 1
        else:
            m = _HUNK_RE.match(l)
            if m:
                old_left = int(m.group(1)) if m.group(1) is not None else 1
                new_left = int(m.group(2)) if m.group(2) is not None else 1
                i += 1
                while i < n and (old_left > 0 or new_left > 0):
                    h = lines[i]
                    if h.startswith("\\"):
                        i += 1
                        continue
                    c = h[:1]
                    if c == "+":
                        info.added += 1
                        new_left -= 1
                    elif c == "-":
                        info.removed += 1
                        old_left -= 1
                    else:
                        old_left -= 1
                        new_left -= 1
                    i += 1
                continue
        i += 1
    info.touched = touched
    if info.is_mbox and info.commits == 0:
        info.commits = 1
    if not info.files:
        info.error = "這不是有效的 git 補丁（找不到任何 diff 內容）"
    return info


def find_recent_patch(repo: str) -> Optional[Path]:
    cands: List[Path] = []
    for folder in (Path(repo).expanduser() if repo else None, Path.home() / "Downloads"):
        if folder and folder.is_dir():
            for pat in ("*.patch", "*.diff"):
                cands.extend(folder.glob(pat))
    cands = [p for p in cands if p.is_file() and time.time() - p.stat().st_mtime < 6 * 3600]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


# ---------------------------------------------------------------------------
# 4. 推送工作（背景執行緒）
# ---------------------------------------------------------------------------
class RepoLock:
    """避免兩個視窗同時處理同一個專案。"""

    def __init__(self, repo: Path) -> None:
        h = hashlib.sha1(str(repo).lower().encode("utf-8")).hexdigest()[:16]
        self.path = config_dir() / "locks" / f"{h}.lock"

    def __enter__(self) -> "RepoLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 900:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                raise UserError("另一個 Patch Pusher 視窗正在處理同一個專案，請稍候再試。")
        raise UserError("無法取得專案鎖定。")

    def __exit__(self, *_: Any) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


def _display_cmd(args: List[str]) -> str:
    def q(a: str) -> str:
        return f'"{a}"' if re.search(r'[\s"&|<>^]', a) else a
    return "git " + " ".join(q(a) for a in args)


class PushJob:
    """安裝補丁 → 提交 → 推送。透過 emit() 把進度與輸出送回 UI 執行緒。"""

    def __init__(self, cfg: Dict[str, Any], info: PatchInfo,
                 emit: Callable[..., None]) -> None:
        self.cfg = cfg
        self.info = info
        self.emit = emit
        self.git_exe = ""
        self.repo = Path()
        self.env: Dict[str, str] = {}
        self.ident: List[str] = []
        self.branch = ""
        self.remote = "origin"
        self.orig_ref = ""
        self.switched = False
        self.created_branch = False
        self.committed = False
        self.stashed = False
        self.stash_overlap = False
        self.note = ""

    # -- 基礎 --------------------------------------------------------------
    def log(self, text: str, tag: str = "meta") -> None:
        self.emit("LOG", text=text + "\n", tag=tag)

    def progress(self, v: float, text: str) -> None:
        self.emit("PROGRESS", value=v, text=text)

    def git(self, args: List[str], timeout: int = 60, echo: bool = True) -> Tuple[int, str]:
        full = [self.git_exe] + self.ident + args
        if echo:
            self.log("$ " + _display_cmd(args), "cmd")
        proc = subprocess.Popen(full, cwd=str(self.repo), env=self.env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
        timed_out = {"v": False}

        def _kill() -> None:
            timed_out["v"] = True
            try:
                proc.kill()
            except Exception:
                pass

        timer = threading.Timer(timeout, _kill)
        timer.start()
        chunks: List[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                chunks.append(line)
                if echo:
                    self.emit("LOG", text=line, tag="out")
            proc.wait()
        finally:
            timer.cancel()
        out = "".join(chunks)
        if timed_out["v"]:
            out += "\n(操作逾時，已中止 — timed out)"
            return 124, out
        return proc.returncode, out

    def must(self, args: List[str], what: str, timeout: int = 60) -> str:
        rc, out = self.git(args, timeout=timeout)
        if rc != 0:
            hint = explain_git_failure(out)
            raise UserError(f"{what}失敗。" + (f"\n{hint}" if hint else f"\n{out.strip()[-400:]}"))
        return out

    # -- 主流程 ------------------------------------------------------------
    def run(self) -> None:
        result: Dict[str, Any] = {"ok": False, "message": "", "sha": "", "url": "", "note": ""}
        tmp_dir: Optional[Path] = None
        try:
            patch_file, tmp_dir = prepare_patch_file(self.info.path)
            self._run(patch_file, result)
            result["ok"] = True
        except UserError as e:
            result["message"] = str(e)
            self.log("✗ " + str(e), "err")
        except Exception as e:  # noqa: BLE001
            result["message"] = f"未預期的錯誤：{e}"
            self.log("✗ " + result["message"], "err")
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            result["note"] = self.note
            self.emit("DONE", **result)

    def _run(self, patch_file: Path, result: Dict[str, Any]) -> None:
        cfg = self.cfg
        self.progress(0.04, "檢查環境…")
        git = find_git()
        if not git:
            raise UserError("找不到 Git。請先安裝 Git for Windows（https://git-scm.com）。")
        self.git_exe = git
        self.repo = Path(cfg["repo_path"]).expanduser().resolve()
        if not self.repo.is_dir():
            raise UserError(f"找不到專案資料夾：{self.repo}")
        if not self.info.path.exists():
            raise UserError(f"找不到補丁檔：{self.info.path}")
        self.remote = (cfg.get("remote") or "origin").strip()
        self.branch = cfg["branch"].strip()
        url = cfg["ssh_url"].strip()
        self.env = build_git_env(cfg.get("ssh_key", ""))
        key = normalize_key(cfg.get("ssh_key", ""))
        if key and not Path(key).expanduser().is_file():
            raise UserError(f"找不到 SSH 私鑰檔案：{key}")
        n, e = cfg.get("user_name", "").strip(), cfg.get("user_email", "").strip()
        if n:
            self.ident += ["-c", f"user.name={n}"]
        if e:
            self.ident += ["-c", f"user.email={e}"]

        with RepoLock(self.repo):
            self.log(f"# 專案：{self.repo}", "meta")
            rc, top = self.git(["rev-parse", "--show-toplevel"], echo=False)
            if rc != 0:
                raise UserError(f"這個資料夾不是 Git 專案：{self.repo}\n請到「設定」重新選擇。")
            rc, gd = self.git(["rev-parse", "--git-dir"], echo=False)
            gitdir = (self.repo / gd.strip()).resolve()
            for marker, label in (("rebase-apply", "git am / rebase"), ("rebase-merge", "rebase"),
                                  ("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick")):
                if (gitdir / marker).exists():
                    raise UserError(f"專案正處於未完成的 {label} 狀態，請先在終端機處理完成（例如 git am --abort）。")

            # 遠端設定與連線驗證（還沒動到任何檔案前先確認）
            self.progress(0.14, "設定並驗證 GitHub 遠端…")
            rc, cur_url = self.git(["remote", "get-url", self.remote], echo=False)
            if rc != 0:
                self.must(["remote", "add", self.remote, url], "新增遠端")
            elif cur_url.strip() != url:
                self.must(["remote", "set-url", self.remote, url], "更新遠端位址")
            rc, out = self.git(["ls-remote", "--heads", self.remote, f"refs/heads/{self.branch}"], timeout=60)
            if rc != 0:
                hint = explain_git_failure(out)
                raise UserError("無法連線到 GitHub。" + (f"\n{hint}" if hint else f"\n{out.strip()[-300:]}"))
            remote_has = bool(re.search(rf"^[0-9a-f]{{40}}\s+refs/heads/{re.escape(self.branch)}$", out, re.M))
            if remote_has:
                self.progress(0.28, "同步遠端分支…")
                self.must(["fetch", self.remote, f"+refs/heads/{self.branch}:refs/remotes/{self.remote}/{self.branch}"],
                          "同步遠端", timeout=120)

            self._guard_local_changes()

            try:
                self.progress(0.50, f"切換到分支 {self.branch}…")
                self._switch_branch(remote_has)
                self.progress(0.64, "套用補丁…")
                mode = self._apply(patch_file)
                self.progress(0.80, "建立提交…")
                self._commit(mode)
            except BaseException:
                if not self.committed:
                    self._rollback_branch()
                self._restore_stash()
                raise

            self.progress(0.90, f"推送到 {self.remote}/{self.branch}…")
            rc, out = self.git(["push", "-u", self.remote, self.branch], timeout=180)
            if rc != 0:
                self._restore_stash()
                hint = explain_git_failure(out)
                raise UserError("補丁已安裝到本地並提交，但推送到 GitHub 失敗。"
                                + (f"\n{hint}" if hint else f"\n{out.strip()[-300:]}"))
            self._restore_stash()

            rc, sha = self.git(["rev-parse", "HEAD"], echo=False)
            sha = sha.strip() if rc == 0 else ""
            result["sha"] = sha
            result["url"] = github_commit_url(url, sha)
            short = sha[:7]
            result["message"] = (f"已推送到 {self.remote}/{self.branch}" + (f" · {short}" if short else "")
                                 if mode != "already" else f"補丁內容已存在，{self.remote}/{self.branch} 已是最新")
            self.log("✓ " + result["message"], "ok")

        if cfg.get("archive"):
            try:
                dest_dir = self.info.path.parent / "done_patches"
                dest_dir.mkdir(exist_ok=True)
                dest = dest_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{self.info.path.name}"
                shutil.move(str(self.info.path), str(dest))
                self.log(f"# 已歸檔補丁：{dest}", "meta")
            except Exception as ex:  # noqa: BLE001
                self.log(f"# 補丁歸檔失敗（不影響結果）：{ex}", "meta")

    # -- 子步驟 ------------------------------------------------------------
    def _guard_local_changes(self) -> None:
        self.progress(0.38, "檢查本地未提交的變更…")
        rc, out = self.git(["status", "--porcelain", "-z", "--untracked-files=all"], echo=False)
        if rc != 0:
            raise UserError("無法讀取專案狀態：\n" + out[-300:])
        touched = set(self.info.touched)
        staged: List[str] = []
        overlap: List[str] = []
        ents = [x for x in out.split("\0") if x]
        i = 0
        while i < len(ents):
            e = ents[i]
            i += 1
            if len(e) < 4:
                continue
            x, y, p = e[0], e[1], e[3:]
            if x in "RC":
                i += 1
            if "U" in (x, y) or (x == "A" and y == "A") or (x == "D" and y == "D"):
                raise UserError(f"專案有尚未解決的合併衝突（{p}），請先處理。")
            if x not in " ?!":
                staged.append(p)
            if p in touched:
                overlap.append(p)
        if not staged and not overlap:
            return
        files = list(dict.fromkeys(staged + overlap))
        reply: Dict[str, str] = {}
        ev = threading.Event()
        self.emit("ASK", files=files, reply=reply, event=ev)
        ev.wait()
        if reply.get("choice") != "stash":
            raise UserError("已取消：你的本地變更沒有被更動。")
        msg = f"PatchPusher auto-stash {datetime.now():%Y-%m-%d %H:%M:%S}"
        self.must(["stash", "push", "--include-untracked", "-m", msg], "暫存本地變更")
        self.stashed = True
        self.stash_overlap = bool(overlap)

    def _switch_branch(self, remote_has: bool) -> None:
        rc, head = self.git(["rev-parse", "--verify", "-q", "HEAD"], echo=False)
        head = head.strip() if rc == 0 else ""
        rc, cur = self.git(["symbolic-ref", "--short", "-q", "HEAD"], echo=False)
        cur = cur.strip() if rc == 0 else ""
        self.orig_ref = cur or head
        rc, _ = self.git(["rev-parse", "--verify", "-q", f"refs/heads/{self.branch}"], echo=False)
        local_has = rc == 0
        if cur != self.branch:
            if local_has:
                self.must(["checkout", self.branch], "切換分支")
            elif remote_has:
                self.must(["checkout", "-b", self.branch, "--track", f"{self.remote}/{self.branch}"], "建立並切換分支")
                self.created_branch = True
            else:
                self.log(f"# 遠端與本地都沒有 {self.branch}，將從目前位置建立新分支。", "meta")
                self.must(["checkout", "-b", self.branch], "建立分支")
                self.created_branch = True
            self.switched = bool(self.orig_ref)
        if remote_has:
            ref = f"refs/remotes/{self.remote}/{self.branch}"
            rc_b, _ = self.git(["merge-base", "--is-ancestor", "HEAD", ref], echo=False)
            if rc_b == 0:
                rc_s, same = self.git(["rev-parse", "HEAD", ref], echo=False)
                if rc_s == 0 and len(set(same.split())) > 1:
                    self.must(["merge", "--ff-only", ref], "快轉到遠端最新版本")
            else:
                rc_a, _ = self.git(["merge-base", "--is-ancestor", ref, "HEAD"], echo=False)
                if rc_a != 0:
                    self.log("# 注意：本地分支與遠端已分歧，推送可能被拒絕。", "meta")

    def _apply(self, patch_file: Path) -> str:
        pf = str(patch_file)
        if self.info.is_mbox:
            rc, out = self.git(["am", "--3way", "--ignore-whitespace", pf], timeout=120)
            if rc == 0:
                self.committed = True
                return "am"
            self.git(["am", "--abort"], echo=False)
            self.log("# git am 無法套用，改用 git apply 重試…", "meta")
        rc, out = self.git(["apply", "--check", "--ignore-whitespace", pf])
        if rc == 0:
            self.must(["apply", "--index", "--ignore-whitespace", pf], "套用補丁")
            return "apply"
        rc_r, _ = self.git(["apply", "--reverse", "--check", "--ignore-whitespace", pf], echo=False)
        if rc_r == 0:
            self.log("# 這個補丁的內容已經存在於本地檔案中。", "meta")
            return "already"
        self.log("# 嘗試三方合併 (--3way)…", "meta")
        rc3, out3 = self.git(["apply", "--3way", "--ignore-whitespace", pf])
        if rc3 == 0:
            return "apply"
        self._cleanup_touched()
        hint = explain_git_failure(out3)
        raise UserError("補丁與目前的程式碼不相符，無法套用（已還原，沒有改動任何檔案）。\n"
                        "常見原因：補丁是以不同版本為基底產生的。請確認本地分支已是最新。"
                        + (f"\n{hint}" if hint else ""))

    def _cleanup_touched(self) -> None:
        paths = self.info.touched
        if not paths:
            return
        self.git(["reset", "-q", "HEAD", "--"] + paths, echo=False)
        for p in paths:
            rc, _ = self.git(["cat-file", "-e", f"HEAD:{p}"], echo=False)
            if rc == 0:
                self.git(["checkout", "HEAD", "--", p], echo=False)
            else:
                try:
                    (self.repo / p).unlink()
                except OSError:
                    pass

    def _commit(self, mode: str) -> None:
        if mode in ("am", "already"):
            return
        rc, _ = self.git(["diff", "--cached", "--quiet"], echo=False)
        if rc == 0:
            self.log("# 沒有需要提交的變更。", "meta")
            return
        msg = self.info.subject or (self.cfg.get("fallback_msg") or "").strip() or f"chore: apply {self.info.path.name}"
        self.must(["commit", "-m", msg], "建立提交")
        self.committed = True

    def _rollback_branch(self) -> None:
        if self.switched and self.orig_ref:
            self.log("# 發生錯誤，還原到原本的分支…", "meta")
            self.git(["checkout", self.orig_ref], echo=False)
            if self.created_branch:
                self.git(["branch", "-D", self.branch], echo=False)

    def _restore_stash(self) -> None:
        if not self.stashed:
            return
        self.stashed = False
        if self.stash_overlap:
            self.note = "你原本未提交的變更已安全保存在 stash，需要時請執行 git stash pop。"
            self.log("# " + self.note, "meta")
            return
        rc, _ = self.git(["stash", "pop"])
        if rc != 0:
            self.note = "還原暫存變更時發生衝突，內容仍保留在 stash（git stash list）。"
            self.log("# " + self.note, "meta")


# ---------------------------------------------------------------------------
# 5. 應用程式圖示（純 Python 繪製，免外部圖檔）
# ---------------------------------------------------------------------------
def _in_rr(u: float, v: float, x1: float, y1: float, x2: float, y2: float, r: float) -> bool:
    if u < x1 or u > x2 or v < y1 or v > y2:
        return False
    dx = max(x1 + r - u, 0.0, u - (x2 - r))
    dy = max(y1 + r - v, 0.0, v - (y2 - r))
    return dx * dx + dy * dy <= r * r


def render_icon_rgba(size: int) -> bytes:
    ss = 3 if size <= 64 else 2
    buf = bytearray(size * size * 4)
    for py in range(size):
        for px in range(size):
            ra = rr = rg = rb = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    u = (px + (sx + 0.5) / ss) / size
                    v = (py + (sy + 0.5) / ss) / size
                    if not _in_rr(u, v, 0.03, 0.03, 0.97, 0.97, 0.22):
                        continue
                    col = (79 + 50 * v, 70 + 22 * v, 229 + 12 * v)  # indigo → violet
                    if _in_rr(u, v, 0.23, 0.15, 0.77, 0.85, 0.06):
                        col = (247, 248, 252)
                        if 0.44 <= v <= 0.55:
                            col = (209, 250, 229)
                            if 0.30 <= u <= 0.70 and 0.485 <= v <= 0.515:
                                col = (16, 185, 129)
                        elif 0.57 <= v <= 0.68:
                            col = (254, 226, 226)
                            if 0.30 <= u <= 0.70 and 0.605 <= v <= 0.635:
                                col = (239, 68, 68)
                        elif (0.26 <= v <= 0.29 and 0.30 <= u <= 0.60) or \
                             (0.34 <= v <= 0.37 and 0.30 <= u <= 0.68) or \
                             (0.73 <= v <= 0.76 and 0.30 <= u <= 0.56):
                            col = (190, 199, 214)
                    ra += 1
                    rr += col[0]
                    rg += col[1]
                    rb += col[2]
            o = (py * size + px) * 4
            if ra:
                buf[o:o + 4] = bytes((int(rr / ra), int(rg / ra), int(rb / ra), int(255 * ra / (ss * ss))))
    return bytes(buf)


def _png_bytes(size: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + rgba[y * size * 4:(y + 1) * size * 4] for y in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_ico(path: Path, sizes: Tuple[int, ...] = (16, 32, 48, 64, 128, 256)) -> None:
    pngs = [(s, _png_bytes(s, render_icon_rgba(s))) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, blobs = b"", b""
    for s, data in pngs:
        entries += struct.pack("<BBBBHHII", 0 if s >= 256 else s, 0 if s >= 256 else s, 0, 0, 1, 32,
                               len(data), offset + len(blobs))
        blobs += data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + entries + blobs)


# ---------------------------------------------------------------------------
# 6. Windows 整合：安裝為應用程式 / .patch 預設開啟程式
# ---------------------------------------------------------------------------
class WinIntegration:
    PROG_ID = "PatchPusher.PatchFile"
    EXTS = (".patch", ".diff")
    UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\PatchPusher"
    CAP_KEY = r"Software\PatchPusher\Capabilities"
    LNK_NAME = "Patch Pusher.lnk"

    @staticmethod
    def supported() -> bool:
        return IS_WIN

    @staticmethod
    def install_dir() -> Path:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Programs" / "PatchPusher"

    @classmethod
    def is_installed_copy(cls) -> bool:
        try:
            return cls.install_dir().resolve() in SELF_PATH.parents
        except Exception:
            return False

    @staticmethod
    def _pythonw() -> str:
        exe = Path(sys.executable).resolve()
        w = exe.with_name("pythonw.exe")
        return str(w if w.exists() else exe)

    @classmethod
    def _launcher_prefix(cls, d: Path) -> str:
        if FROZEN:
            return f'"{d / "PatchPusher.exe"}"'
        return f'"{cls._pythonw()}" "{d / "patch_pusher.pyw"}"'

    @staticmethod
    def _ps(script: str, env_extra: Dict[str, str]) -> None:
        env = os.environ.copy()
        env.update(env_extra)
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", script], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, creationflags=CREATE_NO_WINDOW)

    @classmethod
    def status(cls) -> Dict[str, Any]:
        st = {"installed": False, "default": False, "current": ""}
        if not IS_WIN:
            return st
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{cls.PROG_ID}\shell\open\command"):
                st["installed"] = True
        except OSError:
            return st
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.patch\UserChoice") as k:
                st["current"] = str(winreg.QueryValueEx(k, "ProgId")[0])
                st["default"] = st["current"] == cls.PROG_ID
        except OSError:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\.patch") as k:
                    st["default"] = str(winreg.QueryValueEx(k, "")[0]) == cls.PROG_ID
            except OSError:
                pass
        return st

    @classmethod
    def install(cls, desktop_shortcut: bool = True) -> Tuple[bool, str]:
        if not IS_WIN:
            return False, "目前只支援 Windows 的安裝與檔案關聯。"
        try:
            import winreg
            d = cls.install_dir()
            d.mkdir(parents=True, exist_ok=True)
            target = d / ("PatchPusher.exe" if FROZEN else "patch_pusher.pyw")
            if SELF_PATH != target.resolve():
                try:
                    shutil.copy2(SELF_PATH, target)
                except PermissionError:
                    raise RuntimeError("無法覆寫已安裝的程式（可能正在執行）。請先關閉其他 Patch Pusher 視窗後重試。")
            ico = d / "PatchPusher.ico"
            write_ico(ico)
            prefix = cls._launcher_prefix(d)
            open_cmd = f'{prefix} "%1"'

            def setv(path: str, name: str, value: Any, typ: int = winreg.REG_SZ) -> None:
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, name, 0, typ, value)

            # 記住原本的預設程式，解除安裝時還原
            for ext in cls.EXTS:
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}") as k:
                        prev = str(winreg.QueryValueEx(k, "")[0])
                    if prev and prev != cls.PROG_ID:
                        try:
                            winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\PatchPusher\Previous").Close()
                            have = True
                        except OSError:
                            have = False
                        setv(r"Software\PatchPusher\Previous", ext, prev)
                except OSError:
                    pass

            pid = rf"Software\Classes\{cls.PROG_ID}"
            setv(pid, "", "Git Patch File")
            setv(pid + r"\DefaultIcon", "", f'"{ico}"')
            setv(pid + r"\shell\open", "FriendlyAppName", APP_NAME)
            setv(pid + r"\shell\open\command", "", open_cmd)
            for ext in cls.EXTS:
                setv(rf"Software\Classes\{ext}", "", cls.PROG_ID)
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}\OpenWithProgids", 0,
                                        winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, cls.PROG_ID, 0, winreg.REG_NONE, b"")
            # 讓它出現在「設定 → 預設應用程式」
            setv(cls.CAP_KEY, "ApplicationName", APP_NAME)
            setv(cls.CAP_KEY, "ApplicationDescription", "安裝 Git 補丁並推送到 GitHub")
            for ext in cls.EXTS:
                setv(cls.CAP_KEY + r"\FileAssociations", ext, cls.PROG_ID)
            setv(r"Software\RegisteredApplications", APP_ID, cls.CAP_KEY)
            # 「應用程式與功能」中的解除安裝項目
            u = cls.UNINSTALL_KEY
            setv(u, "DisplayName", APP_NAME)
            setv(u, "DisplayVersion", APP_VERSION)
            setv(u, "Publisher", APP_NAME)
            setv(u, "InstallLocation", str(d))
            setv(u, "DisplayIcon", str(ico))
            setv(u, "InstallDate", datetime.now().strftime("%Y%m%d"))
            setv(u, "UninstallString", f"{prefix} --uninstall")
            setv(u, "NoModify", 1, winreg.REG_DWORD)
            setv(u, "NoRepair", 1, winreg.REG_DWORD)
            try:
                ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
            except Exception:
                pass

            ps = (
                "$ws=New-Object -ComObject WScript.Shell;"
                "function Mk($p){$s=$ws.CreateShortcut($p);$s.TargetPath=$env:PP_TARGET;$s.Arguments=$env:PP_ARGS;"
                "$s.WorkingDirectory=$env:PP_DIR;$s.IconLocation=$env:PP_ICON;$s.Description=$env:PP_DESC;$s.Save()};"
                "Mk (Join-Path ([Environment]::GetFolderPath('Programs')) $env:PP_LNK);"
                "if($env:PP_DESKTOP -eq '1'){Mk (Join-Path ([Environment]::GetFolderPath('Desktop')) $env:PP_LNK)}"
            )
            if FROZEN:
                tgt, args = str(target), ""
            else:
                tgt, args = cls._pythonw(), f'"{target}"'
            cls._ps(ps, {"PP_TARGET": tgt, "PP_ARGS": args, "PP_DIR": str(d), "PP_ICON": str(ico),
                         "PP_DESC": "安裝 Git 補丁並推送到 GitHub", "PP_LNK": cls.LNK_NAME,
                         "PP_DESKTOP": "1" if desktop_shortcut else "0"})
            return True, f"已安裝到：{d}\n並已建立開始功能表" + ("與桌面" if desktop_shortcut else "") + "捷徑。"
        except Exception as e:  # noqa: BLE001
            return False, f"安裝失敗：{e}"

    @classmethod
    def uninstall(cls) -> Tuple[bool, str]:
        if not IS_WIN:
            return False, "目前只支援 Windows。"
        try:
            import winreg

            def del_tree(path: str) -> None:
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                        winreg.KEY_ALL_ACCESS) as k:
                        while True:
                            try:
                                sub = winreg.EnumKey(k, 0)
                            except OSError:
                                break
                            del_tree(path + "\\" + sub)
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
                except OSError:
                    pass

            prev: Dict[str, str] = {}
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\PatchPusher\Previous") as k:
                    for i in range(winreg.QueryInfoKey(k)[1]):
                        n, v, _ = winreg.EnumValue(k, i)
                        prev[n] = str(v)
            except OSError:
                pass
            for ext in cls.EXTS:
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}", 0,
                                        winreg.KEY_ALL_ACCESS) as k:
                        cur = str(winreg.QueryValueEx(k, "")[0])
                        if cur == cls.PROG_ID:
                            if ext in prev:
                                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, prev[ext])
                            else:
                                winreg.DeleteValue(k, "")
                    del_val = rf"Software\Classes\{ext}\OpenWithProgids"
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, del_val, 0, winreg.KEY_SET_VALUE) as k:
                        winreg.DeleteValue(k, cls.PROG_ID)
                except OSError:
                    pass
            del_tree(rf"Software\Classes\{cls.PROG_ID}")
            del_tree(r"Software\PatchPusher")
            del_tree(cls.UNINSTALL_KEY)
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\RegisteredApplications", 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, APP_ID)
            except OSError:
                pass
            try:
                ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
            except Exception:
                pass
            cls._ps("foreach($f in 'Programs','Desktop'){$p=Join-Path ([Environment]::GetFolderPath($f)) $env:PP_LNK;"
                    "if(Test-Path $p){Remove-Item $p -Force}}", {"PP_LNK": cls.LNK_NAME})
            d = cls.install_dir()
            if d.exists():
                subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 3 >nul & rmdir /s /q "{d}"', shell=True,
                                 creationflags=CREATE_NO_WINDOW | 0x00000008, close_fds=True)
            return True, f"已解除安裝。設定檔仍保留在 {config_dir()}，若不需要可自行刪除。"
        except Exception as e:  # noqa: BLE001
            return False, f"解除安裝失敗：{e}"


# ---------------------------------------------------------------------------
# 7. UI 元件
# ---------------------------------------------------------------------------
FONTS: Dict[str, tkfont.Font] = {}


def init_fonts(root: tk.Misc) -> None:
    fams = set(tkfont.families(root))

    def pick(cands: List[str], fallback: str) -> str:
        return next((c for c in cands if c in fams), fallback)

    ui = pick(["Microsoft JhengHei UI", "Microsoft JhengHei", "PingFang TC", "Noto Sans CJK TC",
               "Noto Sans TC", "Heiti TC", "Segoe UI"], "TkDefaultFont")
    mono = pick(["Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "Courier New"], "TkFixedFont")
    FONTS["ui"] = tkfont.Font(root, family=ui, size=10)
    FONTS["small"] = tkfont.Font(root, family=ui, size=9)
    FONTS["bold"] = tkfont.Font(root, family=ui, size=10, weight="bold")
    FONTS["title"] = tkfont.Font(root, family=ui, size=15, weight="bold")
    FONTS["h2"] = tkfont.Font(root, family=ui, size=11, weight="bold")
    FONTS["mono"] = tkfont.Font(root, family=mono, size=9)
    FONTS["mono_b"] = tkfont.Font(root, family=mono, size=9, weight="bold")


def fit_text(text: str, font: tkfont.Font, max_px: float, middle: bool = False) -> str:
    if font.measure(text) <= max_px:
        return text
    if middle:
        lo, hi = 1, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            half = mid // 2
            cand = text[:half] + "…" + text[len(text) - (mid - half):]
            if font.measure(cand) <= max_px:
                lo = mid
            else:
                hi = mid - 1
        half = lo // 2
        return text[:half] + "…" + text[len(text) - (lo - half):]
    while text and font.measure(text + "…") > max_px:
        text = text[:-1]
    return text + "…"


class FlatButton(tk.Label):
    STYLES = {
        "primary": ("#6366F1", "#7F82F5", "#FFFFFF"),
        "secondary": ("#202942", "#2B3555", "#F1F5F9"),
        "ghost": ("#161D2E", "#202942", "#B6C0D4"),
        "danger": ("#161D2E", "#3A1F2B", "#F87171"),
    }

    def __init__(self, parent: tk.Misc, text: str, command: Callable[[], None], kind: str = "secondary",
                 **kw: Any) -> None:
        self._bg, self._hover, fg = self.STYLES[kind]
        super().__init__(parent, text=text, bg=self._bg, fg=fg, font=FONTS["ui"], padx=14, pady=7,
                         cursor="hand2", **kw)
        self._cmd = command
        self._enabled = True
        self.bind("<Enter>", lambda _e: self._enabled and self.configure(bg=self._hover))
        self.bind("<Leave>", lambda _e: self.configure(bg=self._bg))
        self.bind("<ButtonRelease-1>", self._click)

    def _click(self, e: tk.Event) -> None:  # type: ignore[type-arg]
        if self._enabled and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self._cmd()

    def set_enabled(self, flag: bool) -> None:
        self._enabled = flag
        self.configure(cursor="hand2" if flag else "arrow", fg="#F1F5F9" if flag else "#6B7790")


# ---------------------------------------------------------------------------
# 8. 舞台：Mac 安裝程式風格的拖曳畫面
# ---------------------------------------------------------------------------
STAGE_W, STAGE_H = 640, 360


class Stage(tk.Canvas):
    """左：補丁圖示　→　右：專案資料夾。把左邊拖到右邊並放開 = 確認安裝並推送。"""

    def __init__(self, parent: tk.Misc, sc: float, on_drop: Callable[[], None],
                 on_pick_patch: Callable[[], None], on_open_settings: Callable[[], None],
                 on_link: Callable[[str], None]) -> None:
        super().__init__(parent, width=int(STAGE_W * sc), height=int(STAGE_H * sc), bg=C["bg"],
                         highlightthickness=0, bd=0)
        self.sc = sc
        self.cb_drop, self.cb_pick, self.cb_settings, self.cb_link = on_drop, on_pick_patch, on_open_settings, on_link
        # 視圖資料
        self.has_patch = False
        self.patch_name = ""
        self.patch_sub = ""
        self.commit_line = ""
        self.dest_name = ""
        self.dest_sub = ""
        self.dest_path = ""
        self.setup_missing: List[str] = []
        self.status_text = ""
        self.notice: Optional[Tuple[str, str]] = None   # (訊息, 顏色)
        self.links: List[Tuple[str, str]] = []           # (id, 文字)
        self.log_open = False
        # 動畫狀態
        self.phase = "idle"   # idle / dragging / dropping / running / success
        self.pos = [0.0, 0.0]
        self.k = 1.0
        self.prog_target = 0.0
        self.prog_shown = 0.0
        self.anim: Optional[Dict[str, Any]] = None
        self._press: Optional[Tuple[str, float, float]] = None
        self._off = (0.0, 0.0)
        self._link_boxes: Dict[str, Tuple[float, float, float, float]] = {}
        self._dirty = True
        self.pos[0], self.pos[1] = self.home
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Motion>", self._on_hover)
        self.after(33, self._loop)

    # -- 座標 --------------------------------------------------------------
    def s(self, v: float) -> float:
        return v * self.sc

    @property
    def home(self) -> Tuple[float, float]:
        return self.s(170), self.s(168)

    @property
    def dest(self) -> Tuple[float, float]:
        return self.s(470), self.s(168)

    def _near(self, x: float, y: float, c: Tuple[float, float], hw: float, hh: float) -> bool:
        return abs(x - c[0]) <= self.s(hw) and abs(y - c[1]) <= self.s(hh)

    def _over_dest(self) -> bool:
        return self._near(self.pos[0], self.pos[1], self.dest, 78, 66)

    # -- 對外 API ----------------------------------------------------------
    def invalidate(self) -> None:
        self._dirty = True

    def reset(self, animate: bool = True) -> None:
        self.phase = "idle"
        self.prog_target = self.prog_shown = 0.0
        self.status_text = ""
        if animate:
            self.pos[0], self.pos[1] = self.home
            self._start_anim(self.home, 0.5, 1.0, 0.22)
        else:
            self.pos[0], self.pos[1] = self.home
            self.k = 1.0
        self._dirty = True

    def set_progress(self, v: float, text: str) -> None:
        self.prog_target = max(0.0, min(1.0, v))
        self.status_text = text
        self._dirty = True

    def finish(self, ok: bool, text: str) -> None:
        if ok:
            self.phase = "success"
            self.prog_target = 1.0
            self.status_text = text
            self.notice = None
        else:
            self.phase = "idle"
            self.prog_target = self.prog_shown = 0.0
            self.status_text = ""
            self.notice = (text, C["red"])
            self.pos[0], self.pos[1] = self.dest
            self.k = 0.45
            self._start_anim(self.home, 1.0, 0.45, 0.35)
        self._dirty = True

    # -- 動畫 --------------------------------------------------------------
    def _start_anim(self, to: Tuple[float, float], k_to: float, k_from: Optional[float], dur: float,
                    then: Optional[Callable[[], None]] = None) -> None:
        if k_from is not None:
            pass
        self.anim = {"t0": time.time(), "dur": dur, "from": (self.pos[0], self.pos[1]), "to": to,
                     "k0": self.k if k_from is None else k_from, "k1": k_to, "then": then}
        # 參數語意：k_to 為目標縮放；k_from 為起始縮放

    def _anim_step(self) -> None:
        a = self.anim
        if not a:
            return
        t = min(1.0, (time.time() - a["t0"]) / max(a["dur"], 0.001))
        e = 1 - (1 - t) ** 3
        self.pos[0] = a["from"][0] + (a["to"][0] - a["from"][0]) * e
        self.pos[1] = a["from"][1] + (a["to"][1] - a["from"][1]) * e
        self.k = a["k0"] + (a["k1"] - a["k0"]) * e
        if t >= 1.0:
            self.anim = None
            if a["then"]:
                a["then"]()

    def _loop(self) -> None:
        try:
            animating = self.anim is not None
            if animating:
                self._anim_step()
            if abs(self.prog_shown - self.prog_target) > 0.002:
                self.prog_shown += (self.prog_target - self.prog_shown) * 0.18
                animating = True
            if animating or self._dirty or self.phase in ("idle", "dragging", "running"):
                self._dirty = False
                self.redraw()
        except tk.TclError:
            return
        self.after(33, self._loop)

    # -- 滑鼠 --------------------------------------------------------------
    def _hit_link(self, x: float, y: float) -> Optional[str]:
        for name, (x1, y1, x2, y2) in self._link_boxes.items():
            if x1 - 6 <= x <= x2 + 6 and y1 - 6 <= y <= y2 + 6:
                return name
        return None

    def _patch_center(self) -> Tuple[float, float]:
        return (self.pos[0], self.pos[1])

    def _on_hover(self, e: tk.Event) -> None:  # type: ignore[type-arg]
        hand = bool(self._hit_link(e.x, e.y))
        if self.phase in ("idle", "error"):
            hand = hand or self._near(e.x, e.y, self.home, 52, 62) or self._near(e.x, e.y, self.dest, 66, 62)
        self.configure(cursor="hand2" if hand else "")

    def _on_press(self, e: tk.Event) -> None:  # type: ignore[type-arg]
        link = self._hit_link(e.x, e.y)
        if link:
            self.cb_link(link)
            return
        if self.phase != "idle" or self.anim:
            return
        if self._near(e.x, e.y, self.home, 52, 62):
            self.notice = None
            if self.has_patch:
                self.phase = "dragging"
                self._off = (e.x - self.pos[0], e.y - self.pos[1])
            self._press = ("patch", e.x, e.y)
        elif self._near(e.x, e.y, self.dest, 66, 62):
            self._press = ("dest", e.x, e.y)
        self._dirty = True

    def _on_motion(self, e: tk.Event) -> None:  # type: ignore[type-arg]
        if self.phase != "dragging":
            return
        self.pos[0] = max(self.s(40), min(self.s(STAGE_W) - self.s(40), e.x - self._off[0]))
        self.pos[1] = max(self.s(60), min(self.s(STAGE_H) - self.s(70), e.y - self._off[1]))
        self._dirty = True

    def _on_release(self, e: tk.Event) -> None:  # type: ignore[type-arg]
        press, self._press = self._press, None
        if self.phase == "dragging":
            moved = math.hypot(e.x - press[1], e.y - press[2]) if press else 99
            if moved < self.s(6):
                self.phase = "idle"
                self.pos[0], self.pos[1] = self.home
                self.cb_pick()
            elif self._over_dest():
                self.phase = "dropping"
                self._start_anim(self.dest, 0.4, self.k, 0.2, then=self._after_drop)
            else:
                self.phase = "idle"
                self._start_anim(self.home, 1.0, self.k, 0.28)
        elif press and press[0] == "patch" and not self.has_patch:
            self.cb_pick()
        elif press and press[0] == "dest" and self._near(e.x, e.y, self.dest, 66, 62):
            self.cb_settings()
        self._dirty = True

    def _after_drop(self) -> None:
        self.phase = "running"
        self.prog_target = self.prog_shown = 0.04
        self.cb_drop()

    def cancel_drop(self) -> None:
        """設定不完整等情況：把補丁彈回原位。"""
        self.phase = "idle"
        self.prog_target = self.prog_shown = 0.0
        self.pos[0], self.pos[1] = self.dest
        self._start_anim(self.home, 1.0, 0.4, 0.3)

    # -- 繪圖 --------------------------------------------------------------
    def _rr(self, x1: float, y1: float, x2: float, y2: float, r: float, **kw: Any) -> int:
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.create_polygon(pts, smooth=True, **kw)

    def _headline(self) -> Tuple[str, str, str]:
        p = self.phase
        if p == "dragging":
            if self._over_dest():
                return "放開即可安裝並推送", C["green"], "套用補丁 → 提交 → 推送到 GitHub"
            return "把補丁拖到右邊的專案資料夾", C["text"], ""
        if p in ("dropping", "running"):
            return "正在安裝…", C["amber"], self.status_text
        if p == "success":
            return "完成", C["green"], self.status_text
        if self.notice:
            return "沒有完成", C["red"], self.notice[0]
        if not self.has_patch:
            return "選擇要安裝的補丁", C["text"], "點左邊的圖示選擇 .patch 檔，或直接雙擊 .patch 檔案"
        if self.setup_missing:
            return "先完成設定", C["amber"], "請到「⚙ 設定」填寫：" + "、".join(self.setup_missing)
        return "把補丁拖到右邊的專案資料夾", C["text"], "自動套用 → 提交 → 推送到 GitHub"

    def _draw_patch(self, cx: float, cy: float, k: float, lifted: bool) -> None:
        s = self.sc * k
        w, h, r, f = 78 * s, 98 * s, 7 * s, 20 * s
        x1, y1, x2, y2 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        if lifted:
            self._rr(x1 - 7 * s, y1 - 7 * s, x2 + 7 * s, y2 + 7 * s, 14 * s, fill="", outline=C["accent_hi"], width=2)
        self.create_polygon([x1 + r, y1, x2 - f, y1, x2, y1 + f, x2, y2 - r, x2 - r, y2, x1 + r, y2, x1, y2 - r, x1, y1 + r],
                            fill="#F5F7FB", outline="#CBD5E1")
        self.create_polygon([x2 - f, y1, x2 - f, y1 + f, x2, y1 + f], fill="#CBD5E1", outline="#CBD5E1")
        for y, ln, col in ((30, .55, "#CBD5E1"), (42, .75, "#CBD5E1"), (88, .5, "#CBD5E1")):
            self.create_rectangle(x1 + 11 * s, y1 + (y - 2) * s, x1 + 11 * s + (w - 22 * s) * ln, y1 + (y + 2) * s,
                                  fill=col, outline="")
        self.create_rectangle(x1 + 1, y1 + 53 * s, x2 - 1, y1 + 65 * s, fill="#D1FAE5", outline="")
        self.create_rectangle(x1 + 11 * s, y1 + 57.5 * s, x2 - 11 * s, y1 + 60.5 * s, fill="#10B981", outline="")
        self.create_rectangle(x1 + 1, y1 + 67 * s, x2 - 1, y1 + 79 * s, fill="#FEE2E2", outline="")
        self.create_rectangle(x1 + 11 * s, y1 + 71.5 * s, x2 - 11 * s, y1 + 74.5 * s, fill="#EF4444", outline="")

    def _draw_folder(self, cx: float, cy: float, hot: bool, state: str) -> None:
        s = self.sc
        w, h = 112 * s, 84 * s
        x1, y1, x2, y2 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        back, front = ("#4B4FD0", "#6D70F2")
        if state == "success":
            back, front = ("#0E9F6E", "#34D399")
        elif hot:
            back, front = ("#5A5EE8", "#8A8DFF")
        if hot:
            self._rr(x1 - 10 * s, y1 - 10 * s, x2 + 10 * s, y2 + 10 * s, 18 * s, fill="", outline=C["green"], width=3)
        self._rr(x1, y1, x1 + 46 * s, y1 + 26 * s, 8 * s, fill=back, outline="")
        self._rr(x1, y1 + 12 * s, x2, y2, 10 * s, fill=back, outline="")
        self._rr(x1, y1 + 24 * s, x2, y2, 10 * s, fill=front, outline="")
        # 分支圖示
        gx, gy = cx, y1 + 56 * s
        lw = max(2, int(3 * s))
        wh = "#F8FAFC"
        self.create_line(gx - 12 * s, gy - 14 * s, gx - 12 * s, gy + 14 * s, fill=wh, width=lw)
        self.create_line(gx + 12 * s, gy - 4 * s, gx + 12 * s, gy + 1 * s, gx - 12 * s, gy + 10 * s, fill=wh, width=lw, smooth=True)
        for (px, py) in ((gx - 12 * s, gy - 14 * s), (gx - 12 * s, gy + 14 * s), (gx + 12 * s, gy - 6 * s)):
            self.create_oval(px - 5 * s, py - 5 * s, px + 5 * s, py + 5 * s, fill=front, outline=wh, width=lw)

    def redraw(self) -> None:
        self.delete("all")
        s, W = self.sc, self.s(STAGE_W)
        hl, hl_col, sub = self._headline()
        self.create_text(W / 2, self.s(34), text=hl, fill=hl_col, font=FONTS["title"])
        if sub:
            self.create_text(W / 2, self.s(62), text=fit_text(sub, FONTS["ui"], W - self.s(60)), fill=C["sub"],
                             font=FONTS["ui"], fill_=None) if False else \
                self.create_text(W / 2, self.s(62), text=fit_text(sub, FONTS["ui"], W - self.s(60)),
                                 fill=C["sub"], font=FONTS["ui"])
        hx, hy = self.home
        dx, dy = self.dest
        over = self.phase == "dragging" and self._over_dest()

        # 箭頭
        if self.phase in ("idle", "dragging"):
            t = int(time.time() * 3.2) % 3
            for i in range(3):
                ax = (hx + dx) / 2 + (i - 1) * self.s(30)
                col = C["accent_hi"] if i == t else C["border"]
                self.create_line(ax - self.s(7), dy - self.s(14), ax + self.s(7), dy, ax - self.s(7), dy + self.s(14),
                                 fill=col, width=max(2, int(4 * s)), capstyle="round", joinstyle="round")

        # 資料夾 + 進度環
        state = "success" if self.phase == "success" else ""
        self._draw_folder(dx, dy, over, state)
        if self.phase in ("dropping", "running", "success"):
            R = self.s(74)
            self.create_oval(dx - R, dy - R, dx + R, dy + R, outline=C["border"], width=max(2, int(4 * s)))
            ext = -359.9 * (1.0 if self.phase == "success" else self.prog_shown)
            col = C["green"] if self.phase == "success" else C["accent_hi"]
            self.create_arc(dx - R, dy - R, dx + R, dy + R, start=90, extent=ext, style="arc", outline=col,
                            width=max(3, int(5 * s)))
        if self.phase == "success":
            bx, by, br = dx + self.s(50), dy + self.s(40), self.s(17)
            self.create_oval(bx - br, by - br, bx + br, by + br, fill=C["green"], outline=C["bg"], width=max(2, int(3 * s)))
            self.create_line(bx - br * .45, by + br * .02, bx - br * .1, by + br * .4, bx + br * .5, by - br * .35,
                             fill="#04251C", width=max(3, int(4 * s)), capstyle="round", joinstyle="round")

        # 資料夾標籤
        fy = dy + self.s(78)
        self.create_text(dx, fy, text=fit_text(self.dest_name or "尚未設定專案", FONTS["bold"], self.s(240)),
                         fill=C["text"] if self.dest_name else C["amber"], font=FONTS["bold"])
        self.create_text(dx, fy + self.s(20), text=fit_text(self.dest_sub, FONTS["small"], self.s(240)),
                         fill=C["accent_hi"], font=FONTS["small"])
        if self.dest_path:
            self.create_text(dx, fy + self.s(38), text=fit_text(self.dest_path, FONTS["small"], self.s(250), middle=True),
                             fill=C["muted"], font=FONTS["small"])

        # 補丁
        if self.phase not in ("running", "success") and not (self.phase == "dropping" and self.k <= 0.42):
            if self.has_patch:
                self._draw_patch(self.pos[0], self.pos[1], self.k, self.phase == "dragging")
            else:
                self._draw_empty_patch(hx, hy)
        if self.phase in ("running", "success"):
            pass
        px_label = hx
        if self.phase == "success":
            name, subl, ncol = "已安裝", self.patch_name, C["green"]
        else:
            name, subl, ncol = (self.patch_name or "尚未選擇補丁"), self.patch_sub, C["text"] if self.has_patch else C["amber"]
        self.create_text(px_label, fy, text=fit_text(name, FONTS["bold"], self.s(240)), fill=ncol, font=FONTS["bold"])
        if subl:
            self.create_text(px_label, fy + self.s(20), text=fit_text(subl, FONTS["small"], self.s(240)),
                             fill=C["accent_hi"] if self.phase != "success" else C["sub"], font=FONTS["small"])

        # 提交訊息
        if self.has_patch and self.commit_line and self.phase not in ("success",):
            self.create_text(W / 2, self.s(298), text=fit_text("提交訊息　" + self.commit_line, FONTS["small"], W - self.s(80)),
                             fill=C["muted"], font=FONTS["small"])

        # 連結列
        self._link_boxes = {}
        items = list(self.links) + [("toggle_log", "收合記錄 ▴" if self.log_open else "顯示記錄 ▾")]
        y = self.s(STAGE_H - 22)
        x = W - self.s(18)
        for name, text in reversed(items):
            it = self.create_text(x, y, text=text, fill=C["accent_hi"] if name != "toggle_log" else C["muted"],
                                  font=FONTS["small"], anchor="e")
            bb = self.bbox(it)
            self._link_boxes[name] = (bb[0], bb[1], bb[2], bb[3])
            x = bb[0] - self.s(22)

    def _draw_empty_patch(self, cx: float, cy: float) -> None:
        s = self.sc
        w, h = 78 * s, 98 * s
        self._rr(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, 10 * s, fill="", outline=C["muted"], width=2, dash=(6, 5))
        self.create_line(cx - 14 * s, cy, cx + 14 * s, cy, fill=C["muted"], width=max(2, int(3 * s)), capstyle="round")
        self.create_line(cx, cy - 14 * s, cx, cy + 14 * s, fill=C["muted"], width=max(2, int(3 * s)), capstyle="round")


# ---------------------------------------------------------------------------
# 9. 設定視窗
# ---------------------------------------------------------------------------
class SettingsDialog(tk.Toplevel):
    def __init__(self, app: "PatchPusherApp", first_run: bool = False) -> None:
        super().__init__(app)
        self.app = app
        self.title("設定")
        self.configure(bg=C["bg"])
        self.transient(app)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)
        cfg = app.cfg
        self.v: Dict[str, Any] = {
            "repo_path": tk.StringVar(value=cfg["repo_path"]), "ssh_url": tk.StringVar(value=cfg["ssh_url"]),
            "branch": tk.StringVar(value=cfg["branch"]), "user_name": tk.StringVar(value=cfg["user_name"]),
            "user_email": tk.StringVar(value=cfg["user_email"]), "ssh_key": tk.StringVar(value=cfg["ssh_key"]),
            "remote": tk.StringVar(value=cfg["remote"]), "fallback_msg": tk.StringVar(value=cfg["fallback_msg"]),
            "archive": tk.BooleanVar(value=cfg["archive"]), "auto_close": tk.BooleanVar(value=cfg["auto_close"]),
        }
        self._test_q: "queue.Queue[Tuple[bool, str]]" = queue.Queue()
        body = tk.Frame(self, bg=C["bg"], padx=26, pady=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        r = 0
        tk.Label(body, text="推送設定", bg=C["bg"], fg=C["text"], font=FONTS["title"]).grid(row=r, column=0, sticky="w")
        r += 1
        intro = "填一次就好。之後雙擊 .patch，拖一下就完成。" if not first_run else "第一次使用：先填好下面三項必要資訊。"
        tk.Label(body, text=intro, bg=C["bg"], fg=C["sub"], font=FONTS["ui"]).grid(row=r, column=0, sticky="w", pady=(2, 14))
        r += 1
        r = self._field(body, r, "專案資料夾（補丁會安裝到這裡）", "repo_path", browse="dir")
        r = self._field(body, r, "GitHub SSH 位址", "ssh_url", hint="例：git@github.com:帳號/專案.git")
        r = self._field(body, r, "推送分支", "branch", hint="例：main、dev/gemini")
        r = self._field(body, r, "SSH 私鑰（選填）", "ssh_key", browse="key", hint="留空 = 用系統預設 / ssh-agent；請選沒有 .pub 的那個檔")

        who = tk.Frame(body, bg=C["bg"])
        who.grid(row=r, column=0, sticky="ew", pady=(0, 6))
        who.columnconfigure(0, weight=1)
        who.columnconfigure(1, weight=1)
        for col, (lab, key) in enumerate((("Git 名稱（選填）", "user_name"), ("Git 信箱（選填）", "user_email"))):
            cell = tk.Frame(who, bg=C["bg"])
            cell.grid(row=0, column=col, sticky="ew", padx=(0, 8) if col == 0 else (8, 0))
            tk.Label(cell, text=lab, bg=C["bg"], fg=C["text"], font=FONTS["bold"]).pack(anchor="w")
            self._entry(cell, key).pack(fill="x", ipady=5, pady=(3, 0))
        r += 1
        tk.Label(body, text="名稱與信箱留空時，使用這台電腦現有的 Git 設定。", bg=C["bg"], fg=C["muted"],
                 font=FONTS["small"]).grid(row=r, column=0, sticky="w", pady=(0, 10))
        r += 1

        trow = tk.Frame(body, bg=C["bg"])
        trow.grid(row=r, column=0, sticky="ew", pady=(0, 6))
        self.btn_test = FlatButton(trow, "測試 GitHub 連線", self._test)
        self.btn_test.pack(side="left")
        self.lbl_test = tk.Label(trow, text="", bg=C["bg"], fg=C["sub"], font=FONTS["small"], wraplength=360, justify="left")
        self.lbl_test.pack(side="left", padx=12)
        r += 1

        # 進階
        self._adv_open = False
        self.btn_adv = tk.Label(body, text="進階選項 ▸", bg=C["bg"], fg=C["accent_hi"], font=FONTS["small"], cursor="hand2")
        self.btn_adv.grid(row=r, column=0, sticky="w", pady=(4, 0))
        self.btn_adv.bind("<Button-1>", lambda _e: self._toggle_adv())
        r += 1
        self.adv = tk.Frame(body, bg=C["bg"])
        self.adv.grid(row=r, column=0, sticky="ew")
        self.adv.grid_remove()
        self._field(self.adv, 0, "遠端名稱", "remote")
        self._field(self.adv, 1, "沒有標題的補丁使用此提交訊息", "fallback_msg")
        for i, (t, k) in enumerate((("推送成功後，把 .patch 移到 done_patches 資料夾", "archive"),
                                    ("完成後自動關閉視窗", "auto_close"))):
            tk.Checkbutton(self.adv, text=t, variable=self.v[k], bg=C["bg"], fg=C["sub"], selectcolor=C["panel"],
                           activebackground=C["bg"], activeforeground=C["text"], font=FONTS["small"]
                           ).grid(row=2 + i, column=0, sticky="w")
        self.adv.columnconfigure(0, weight=1)
        r += 1

        # 應用程式整合
        sep = tk.Frame(body, bg=C["border"], height=1)
        sep.grid(row=r, column=0, sticky="ew", pady=14)
        r += 1
        tk.Label(body, text="應用程式", bg=C["bg"], fg=C["text"], font=FONTS["h2"]).grid(row=r, column=0, sticky="w")
        r += 1
        self.lbl_app = tk.Label(body, text="", bg=C["bg"], fg=C["sub"], font=FONTS["ui"], wraplength=520, justify="left")
        self.lbl_app.grid(row=r, column=0, sticky="w", pady=(4, 6))
        r += 1
        self.app_btns = tk.Frame(body, bg=C["bg"])
        self.app_btns.grid(row=r, column=0, sticky="w")
        r += 1
        self.desk_var = tk.BooleanVar(value=True)
        self.chk_desk = tk.Checkbutton(body, text="同時建立桌面捷徑", variable=self.desk_var, bg=C["bg"], fg=C["sub"],
                                       selectcolor=C["panel"], activebackground=C["bg"], activeforeground=C["text"],
                                       font=FONTS["small"])
        self.chk_desk.grid(row=r, column=0, sticky="w", pady=(6, 0))
        r += 1
        self._refresh_app_section()

        self.lbl_err = tk.Label(body, text="", bg=C["bg"], fg=C["red"], font=FONTS["small"], wraplength=520, justify="left")
        self.lbl_err.grid(row=r, column=0, sticky="w", pady=(12, 0))
        r += 1
        bar = tk.Frame(body, bg=C["bg"])
        bar.grid(row=r, column=0, sticky="e", pady=(8, 0))
        FlatButton(bar, "取消", self._close, kind="ghost").pack(side="left", padx=(0, 8))
        FlatButton(bar, "儲存", self._save, kind="primary").pack(side="left")

        self.update_idletasks()
        x = app.winfo_rootx() + max(0, (app.winfo_width() - self.winfo_reqwidth()) // 2)
        y = max(10, app.winfo_rooty() - 20)
        self.geometry(f"+{x}+{y}")
        self.after(80, self._poll_test)
        self.grab_set()

    # -- 小工具 ------------------------------------------------------------
    def _entry(self, parent: tk.Misc, key: str) -> tk.Entry:
        return tk.Entry(parent, textvariable=self.v[key], bg=C["panel"], fg=C["text"], insertbackground="#FFFFFF",
                        relief="flat", font=FONTS["mono"], highlightthickness=1, highlightbackground=C["border"],
                        highlightcolor=C["accent"], width=54)

    def _field(self, parent: tk.Misc, row: int, label: str, key: str, browse: str = "", hint: str = "") -> int:
        wrap = tk.Frame(parent, bg=C["bg"])
        wrap.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        wrap.columnconfigure(0, weight=1)
        tk.Label(wrap, text=label, bg=C["bg"], fg=C["text"], font=FONTS["bold"]).grid(row=0, column=0, sticky="w")
        e = self._entry(wrap, key)
        e.grid(row=1, column=0, sticky="ew", ipady=5, pady=(3, 0))
        if browse:
            FlatButton(wrap, "瀏覽…", lambda: self._browse(browse), kind="secondary").grid(row=1, column=1, padx=(8, 0), pady=(3, 0))
        if hint:
            tk.Label(wrap, text=hint, bg=C["bg"], fg=C["muted"], font=FONTS["small"]).grid(row=2, column=0, sticky="w", pady=(2, 0))
        return row + 1

    def _browse(self, mode: str) -> None:
        if mode == "dir":
            cur = self.v["repo_path"].get()
            d = filedialog.askdirectory(parent=self, initialdir=cur if os.path.isdir(cur) else str(Path.home()),
                                        title="選擇補丁要安裝到的專案資料夾")
            if d:
                self.v["repo_path"].set(d)
                self._autofill(d)
        else:
            ssh = Path.home() / ".ssh"
            f = filedialog.askopenfilename(parent=self, title="選擇 SSH 私鑰（例如 id_ed25519，不要選 .pub）",
                                           initialdir=str(ssh if ssh.is_dir() else Path.home()))
            if f:
                self.v["ssh_key"].set(normalize_key(f))

    def _autofill(self, d: str) -> None:
        rc, url = quick_git(["remote", "get-url", self.v["remote"].get().strip() or "origin"], cwd=Path(d))
        if rc == 0 and not self.v["ssh_url"].get().strip():
            self.v["ssh_url"].set(url)
        rc, br = quick_git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=Path(d))
        if rc == 0 and br and not self.v["branch"].get().strip():
            self.v["branch"].set(br)

    def _toggle_adv(self) -> None:
        self._adv_open = not self._adv_open
        if self._adv_open:
            self.adv.grid()
            self.btn_adv.configure(text="進階選項 ▾")
        else:
            self.adv.grid_remove()
            self.btn_adv.configure(text="進階選項 ▸")

    # -- 連線測試 ----------------------------------------------------------
    def _test(self) -> None:
        url = self.v["ssh_url"].get().strip()
        if not url:
            self.lbl_test.configure(text="請先填寫 GitHub SSH 位址", fg=C["amber"])
            return
        if not find_git():
            self.lbl_test.configure(text="找不到 Git，請先安裝 Git for Windows", fg=C["red"])
            return
        key = normalize_key(self.v["ssh_key"].get())
        branch = self.v["branch"].get().strip()
        self.lbl_test.configure(text="測試中…", fg=C["amber"])
        self.btn_test.set_enabled(False)

        def work() -> None:
            if key and not Path(key).expanduser().is_file():
                self._test_q.put((False, f"找不到私鑰檔案：{key}"))
                return
            args = ["ls-remote", "--heads", url] + ([f"refs/heads/{branch}"] if branch else [])
            rc, out = quick_git(args, cwd=config_dir() if config_dir().is_dir() else Path.home(), timeout=60,
                                env=build_git_env(key))
            if rc != 0:
                self._test_q.put((False, explain_git_failure(out) or out[-200:] or "連線失敗"))
            elif branch and re.search(rf"refs/heads/{re.escape(branch)}$", out, re.M):
                self._test_q.put((True, f"連線成功，遠端已有分支 {branch}"))
            elif branch:
                self._test_q.put((True, f"連線成功；遠端還沒有 {branch}，第一次推送時會建立"))
            else:
                self._test_q.put((True, "連線成功"))
        threading.Thread(target=work, daemon=True).start()

    def _poll_test(self) -> None:
        try:
            while True:
                ok, msg = self._test_q.get_nowait()
                self.lbl_test.configure(text=("✓ " if ok else "✗ ") + msg, fg=C["green"] if ok else C["red"])
                self.btn_test.set_enabled(True)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self.after(150, self._poll_test)

    # -- 應用程式整合 ------------------------------------------------------
    def _refresh_app_section(self) -> None:
        for w in self.app_btns.winfo_children():
            w.destroy()
        if not WinIntegration.supported():
            self.lbl_app.configure(text="安裝為應用程式與 .patch 檔案關聯目前只支援 Windows。")
            self.chk_desk.grid_remove()
            return
        st = WinIntegration.status()
        note = "" if FROZEN else "\n（目前以 Python 腳本執行；若想免裝 Python，可用 build_windows.bat 打包成 PatchPusher.exe 後再安裝。）"
        if not st["installed"]:
            self.lbl_app.configure(text="尚未安裝。安裝後可從開始功能表開啟，並讓 .patch / .diff 檔雙擊就開啟本工具。" + note)
            FlatButton(self.app_btns, "安裝為應用程式並設為 .patch 預設", self._do_install, kind="primary").pack(side="left")
        else:
            if st["default"]:
                txt = "✓ 已安裝，雙擊 .patch / .diff 檔會直接開啟本工具。"
            else:
                cur = st["current"] or "其他程式"
                txt = (f"已安裝，但 Windows 目前的 .patch 預設程式是「{cur}」。\n"
                       "Windows 不允許程式自行覆蓋你已選的預設，請在設定裡手動選一次 Patch Pusher。")
            self.lbl_app.configure(text=txt + note)
            if not st["default"]:
                FlatButton(self.app_btns, "開啟 Windows 預設應用程式設定", self._open_defaults, kind="primary").pack(side="left", padx=(0, 8))
            FlatButton(self.app_btns, "重新安裝 / 修復", self._do_install).pack(side="left", padx=(0, 8))
            FlatButton(self.app_btns, "解除安裝", self._do_uninstall, kind="danger").pack(side="left")

    def _open_defaults(self) -> None:
        try:
            os.startfile("ms-settings:defaultapps")  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo(APP_NAME, "請到「設定 → 應用程式 → 預設應用程式」，搜尋 .patch 並選擇 Patch Pusher。", parent=self)

    def _do_install(self) -> None:
        ok, msg = WinIntegration.install(self.desk_var.get())
        if ok:
            st = WinIntegration.status()
            if not st["default"]:
                msg += ("\n\n最後一步：Windows 10/11 要由你確認預設程式。\n"
                        "對任一 .patch 檔按右鍵 → 開啟檔案 → 選擇其他應用程式 → Patch Pusher → 勾選「一律使用」。")
            messagebox.showinfo(APP_NAME, msg, parent=self)
        else:
            messagebox.showerror(APP_NAME, msg, parent=self)
        self._refresh_app_section()

    def _do_uninstall(self) -> None:
        if not messagebox.askyesno(APP_NAME, "確定要解除安裝 Patch Pusher 嗎？", parent=self):
            return
        ok, msg = WinIntegration.uninstall()
        (messagebox.showinfo if ok else messagebox.showerror)(APP_NAME, msg, parent=self)
        self._refresh_app_section()

    # -- 儲存 --------------------------------------------------------------
    def _save(self) -> None:
        g = {k: (v.get().strip() if isinstance(v, tk.StringVar) else bool(v.get())) for k, v in self.v.items()}
        g["ssh_key"] = normalize_key(g["ssh_key"])
        err = ""
        if not g["repo_path"] or not Path(g["repo_path"]).expanduser().is_dir():
            err = "專案資料夾不存在，請重新選擇。"
        elif find_git() and quick_git(["rev-parse", "--is-inside-work-tree"], cwd=Path(g["repo_path"]).expanduser())[0] != 0:
            err = "這個資料夾不是 Git 專案（找不到 .git）。"
        elif not g["ssh_url"]:
            err = "請填寫 GitHub SSH 位址。"
        elif not g["branch"]:
            err = "請填寫推送分支。"
        elif g["ssh_key"] and not Path(g["ssh_key"]).expanduser().is_file():
            err = "找不到 SSH 私鑰檔案。"
        if err:
            self.lbl_err.configure(text="⚠ " + err)
            return
        g["remote"] = g["remote"] or "origin"
        g["fallback_msg"] = g["fallback_msg"] or DEFAULT_CFG["fallback_msg"]
        self.app.cfg.update(g)
        try:
            save_config(self.app.cfg)
        except Exception as e:  # noqa: BLE001
            self.lbl_err.configure(text=f"⚠ 無法儲存設定：{e}")
            return
        self._close()
        self.app.on_config_changed()

    def _close(self) -> None:
        self.app.settings_win = None
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


# ---------------------------------------------------------------------------
# 10. 主視窗
# ---------------------------------------------------------------------------
def enable_high_dpi() -> None:
    if IS_WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PatchPusher.App")
        except Exception:
            pass


class PatchPusherApp(tk.Tk):
    def __init__(self, initial_patch: Optional[Path] = None) -> None:
        enable_high_dpi()
        super().__init__()
        self.withdraw()
        self.sc = max(1.0, float(self.winfo_fpixels("1i")) / 96.0)
        init_fonts(self)
        self.title(APP_NAME)
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        try:
            png = _png_bytes(64, render_icon_rgba(64))
            self._icon = tk.PhotoImage(data=base64.b64encode(png))
            self.iconphoto(True, self._icon)
        except Exception:
            pass

        self.cfg, existed = load_config()
        self.queue: "queue.Queue[Tuple[str, Dict[str, Any]]]" = queue.Queue()
        self.busy = False
        self.patch: Optional[PatchInfo] = None
        self.settings_win: Optional[SettingsDialog] = None
        self.last_url = ""
        self.log_open = False
        self.report_callback_exception = self._on_tk_error  # type: ignore[assignment]

        menubar = tk.Menu(self, tearoff=0)
        menubar.add_command(label="⚙ 設定", command=self.open_settings)
        self.config(menu=menubar)

        self.stage = Stage(self, self.sc, on_drop=self._start_job, on_pick_patch=self.choose_patch,
                           on_open_settings=self.open_settings, on_link=self._on_link)
        self.stage.pack(fill="x")
        self.log_frame = tk.Frame(self, bg=C["panel"], padx=10, pady=8)
        self.log_text = tk.Text(self.log_frame, height=10, bg=C["panel"], fg=C["sub"], relief="flat", font=FONTS["mono"],
                                wrap="word", padx=6, pady=4, highlightthickness=0, state="disabled")
        sb = tk.Scrollbar(self.log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        for tag, col in (("cmd", "#22D3EE"), ("out", C["text"]), ("meta", C["accent_hi"]), ("err", C["red"]), ("ok", C["green"])):
            self.log_text.tag_config(tag, foreground=col)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Control-comma>", lambda _e: self.open_settings())

        if initial_patch is not None:
            self.load_patch(initial_patch)
        else:
            rec = find_recent_patch(self.cfg["repo_path"])
            if rec:
                self.load_patch(rec)
        self.refresh_view()

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = (self.winfo_screenwidth() - w) // 2
        y = max(20, (self.winfo_screenheight() - h) // 3)
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.after(100, self._poll)
        if not existed or (initial_patch is not None and missing_settings(self.cfg)):
            self.after(300, lambda: self.open_settings(first_run=not existed))

    # -- 視圖 --------------------------------------------------------------
    def refresh_view(self) -> None:
        st = self.stage
        p = self.patch
        st.has_patch = bool(p and not p.error)
        if p is None:
            st.patch_name, st.patch_sub, st.commit_line = "", "點擊選擇補丁檔", ""
        else:
            st.patch_name = p.path.name
            if p.error:
                st.patch_sub, st.commit_line = p.error, ""
            else:
                bits = [f"{len(p.files)} 個檔案", f"+{p.added} −{p.removed}"]
                if p.commits > 1:
                    bits.insert(0, f"{p.commits} 個提交")
                st.patch_sub = "　".join(bits)
                st.commit_line = p.subject or self.cfg.get("fallback_msg", "")
        repo = (self.cfg.get("repo_path") or "").strip()
        branch = (self.cfg.get("branch") or "").strip()
        remote = (self.cfg.get("remote") or "origin").strip()
        st.dest_name = Path(repo).name or repo if repo else ""
        st.dest_sub = f"{remote} / {branch}" if branch else "尚未設定分支"
        st.dest_path = repo
        st.setup_missing = missing_settings(self.cfg)
        st.log_open = self.log_open
        st.invalidate()

    def on_config_changed(self) -> None:
        if self.patch is None:
            rec = find_recent_patch(self.cfg["repo_path"])
            if rec:
                self.load_patch(rec)
        self.refresh_view()

    def load_patch(self, path: Path) -> None:
        path = Path(path).expanduser()
        try:
            path = path.resolve()
        except OSError:
            pass
        if not path.is_file():
            self.patch = None
            self.stage.notice = (f"找不到補丁檔：{path}", C["red"])
        else:
            self.patch = inspect_patch(path)
            if self.patch.error:
                self.stage.notice = (self.patch.error, C["red"])
            else:
                self.stage.notice = None
        self.stage.reset(animate=False)
        self.refresh_view()

    def choose_patch(self) -> None:
        if self.busy:
            return
        if self.stage.phase == "success":
            self.stage.reset(animate=False)
        start = Path.home() / "Downloads"
        if self.patch and self.patch.path.parent.is_dir():
            start = self.patch.path.parent
        f = filedialog.askopenfilename(parent=self, title="選擇要安裝的補丁檔", initialdir=str(start),
                                       filetypes=[("Git 補丁", "*.patch *.diff"), ("所有檔案", "*.*")])
        if f:
            self.load_patch(Path(f))

    def open_settings(self, first_run: bool = False) -> None:
        if self.busy:
            messagebox.showinfo(APP_NAME, "正在處理補丁，請等完成後再修改設定。", parent=self)
            return
        if self.settings_win is not None:
            self.settings_win.lift()
            return
        self.settings_win = SettingsDialog(self, first_run=first_run)

    # -- 記錄 --------------------------------------------------------------
    def _log(self, text: str, tag: str = "out") -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text, tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_log(self, show: bool) -> None:
        self.log_open = show
        if show:
            self.log_frame.pack(fill="both", expand=True)
        else:
            self.log_frame.pack_forget()
        self.geometry("")
        self.stage.log_open = show
        self.stage.invalidate()

    def _on_link(self, name: str) -> None:
        if name == "toggle_log":
            self._set_log(not self.log_open)
        elif name == "github" and self.last_url:
            import webbrowser
            webbrowser.open(self.last_url)
        elif name == "again":
            self.patch = None
            self.stage.links = []
            self.stage.reset()
            self.refresh_view()
            self.choose_patch()

    # -- 任務 --------------------------------------------------------------
    def _start_job(self) -> None:
        missing = missing_settings(self.cfg)
        if not self.patch or self.patch.error or missing:
            self.stage.cancel_drop()
            if missing:
                self.open_settings()
            return
        self.busy = True
        self.stage.links = []
        self.stage.notice = None
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        job = PushJob(dict(self.cfg), self.patch, lambda kind, **kw: self.queue.put((kind, kw)))
        threading.Thread(target=job.run, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                kind, p = self.queue.get_nowait()
                if kind == "LOG":
                    self._log(p["text"], p.get("tag", "out"))
                elif kind == "PROGRESS":
                    self.stage.set_progress(p["value"], p["text"])
                elif kind == "ASK":
                    shown = "\n".join(f"  • {f}" for f in p["files"][:8]) + ("\n  …" if len(p["files"]) > 8 else "")
                    ok = messagebox.askyesno(
                        "專案有未提交的變更",
                        f"下列檔案有未提交的變更，會和補丁衝突或已在暫存區：\n\n{shown}\n\n"
                        "要先自動暫存（git stash）再安裝補丁嗎？\n你的變更不會遺失。\n\n選「否」會取消安裝，不動任何檔案。",
                        parent=self)
                    p["reply"]["choice"] = "stash" if ok else "cancel"
                    p["event"].set()
                elif kind == "DONE":
                    self._on_done(p)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _on_done(self, p: Dict[str, Any]) -> None:
        self.busy = False
        msg = p.get("message", "")
        if p.get("ok"):
            self.last_url = p.get("url", "")
            text = msg + (f"\n{p['note']}" if p.get("note") else "")
            self.stage.finish(True, text.split("\n")[0])
            self.stage.links = ([("github", "在 GitHub 檢視 ↗")] if self.last_url else []) + [("again", "安裝另一個補丁")]
            if p.get("note"):
                self._set_log(True)
            if self.cfg.get("auto_close") and not p.get("note"):
                self.after(2500, self.destroy)
        else:
            first = msg.split("\n")[0]
            self.stage.finish(False, first)
            self.stage.links = []
            self._set_log(True)
            if "\n" in msg:
                self._log(msg + "\n", "err")
        self.refresh_view()

    def _on_close(self) -> None:
        if self.busy:
            messagebox.showinfo(APP_NAME, "正在安裝並推送中，請等它完成再關閉（通常只要幾秒）。", parent=self)
            return
        self.destroy()

    def _on_tk_error(self, exc: type, val: BaseException, tb: Any) -> None:
        import traceback
        try:
            config_dir().mkdir(parents=True, exist_ok=True)
            with open(config_dir() / "error.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now():%F %T}]\n" + "".join(traceback.format_exception(exc, val, tb)))
        except Exception:
            pass
        self.busy = False
        messagebox.showerror(APP_NAME, f"發生未預期的錯誤：{val}\n詳細內容已寫入 {config_dir() / 'error.log'}")


# ---------------------------------------------------------------------------
# 11. 進入點
# ---------------------------------------------------------------------------
def _hidden_root() -> tk.Tk:
    r = tk.Tk()
    r.withdraw()
    return r


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("patch", nargs="?")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--export-icon", metavar="PATH")
    args, _ = ap.parse_known_args(sys.argv[1:] if argv is None else argv)

    if args.export_icon:
        write_ico(Path(args.export_icon))
        return 0
    if args.install:
        enable_high_dpi()
        root = _hidden_root()
        ok, msg = WinIntegration.install(True)
        (messagebox.showinfo if ok else messagebox.showerror)(APP_NAME, msg)
        root.destroy()
        return 0 if ok else 1
    if args.uninstall:
        enable_high_dpi()
        root = _hidden_root()
        if messagebox.askyesno(APP_NAME, "確定要解除安裝 Patch Pusher 嗎？"):
            ok, msg = WinIntegration.uninstall()
            (messagebox.showinfo if ok else messagebox.showerror)(APP_NAME, msg)
        root.destroy()
        return 0

    patch = Path(args.patch).expanduser().resolve() if args.patch else None
    PatchPusherApp(initial_patch=patch).mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        import traceback
        try:
            config_dir().mkdir(parents=True, exist_ok=True)
            (config_dir() / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise
