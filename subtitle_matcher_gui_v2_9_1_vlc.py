#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Subtitle Matcher v1.0
A 人工中文字幕 + B AI 中文字幕
將 A 的文字套用到 B 的時間軸，支援手動配對與 AI 預配對。
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
import shutil
import time
import importlib
import re
import threading
import tempfile
import uuid
import tkinter as tk
from dataclasses import dataclass, asdict, field
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None

try:
    import keyring
except ImportError:
    keyring = None



APP_NAME = "SubtitleMatcher"
APP_VERSION = "2.9.1"

def get_app_dir() -> Path:
    """設定、暫存與紀錄固定放在程式／EXE 所在資料夾。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

APP_DIR = get_app_dir()
CONFIG_DIR = APP_DIR
CONFIG_FILE = APP_DIR / "config.json"
AUTOSAVE_FILE = APP_DIR / "autosave_project.json"
TEMP_DIR = APP_DIR / "temp"
LOG_DIR = APP_DIR / "logs"
AI_CHECK_FILE = APP_DIR / "ai_check_result.json"
SERVICE_NAME = "SubtitleMatcherAI"


@dataclass
class SrtEntry:
    number: int
    start_ms: int
    end_ms: int
    text: str
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def start(self) -> str:
        return ms_to_srt(self.start_ms)

    @property
    def end(self) -> str:
        return ms_to_srt(self.end_ms)


@dataclass
class MatchInfo:
    a_index: int
    b_indices: List[int]
    status: str = "manual"
    confidence: float = 1.0
    note: str = ""
    assigned_start_ms: Optional[int] = None
    assigned_end_ms: Optional[int] = None


def srt_to_ms(value: str) -> int:
    value = value.strip().replace(".", ",")
    m = re.match(r"(\d+):(\d+):(\d+),(\d+)", value)
    if not m:
        raise ValueError(f"無法解析時間：{value}")
    h, minute, sec, milli = map(int, m.groups())
    return ((h * 60 + minute) * 60 + sec) * 1000 + milli


def ms_to_srt(value: int) -> str:
    value = max(0, int(value))
    h, remain = divmod(value, 3600000)
    minute, remain = divmod(remain, 60000)
    sec, milli = divmod(remain, 1000)
    return f"{h:02d}:{minute:02d}:{sec:02d},{milli:03d}"


def read_text_auto(path: str) -> str:
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5", "shift_jis"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_srt(path: str) -> List[SrtEntry]:
    raw = read_text_auto(path).replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = re.split(r"\n{2,}", raw)
    entries: List[SrtEntry] = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 2:
            continue
        if "-->" in lines[0]:
            number = len(entries) + 1
            timing_line = lines[0]
            text_lines = lines[1:]
        else:
            try:
                number = int(lines[0].strip())
            except ValueError:
                number = len(entries) + 1
            timing_line = lines[1] if len(lines) > 1 else ""
            text_lines = lines[2:]
        if "-->" not in timing_line:
            continue
        left, right = [x.strip() for x in timing_line.split("-->", 1)]
        right = right.split()[0]
        try:
            start_ms = srt_to_ms(left)
            end_ms = srt_to_ms(right)
        except ValueError:
            continue
        entries.append(SrtEntry(number, start_ms, end_ms, "\n".join(text_lines).strip()))
    if not entries:
        raise ValueError("沒有找到有效的 SRT 字幕。")
    return entries


def write_srt(path: str, entries: List[SrtEntry]) -> None:
    out = []
    for i, e in enumerate(entries, 1):
        out.append(f"{i}\n{ms_to_srt(e.start_ms)} --> {ms_to_srt(e.end_ms)}\n{e.text}\n")
    Path(path).write_text("\n".join(out), encoding="utf-8-sig")


def clean_json_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    first_arr, last_arr = text.find("["), text.rfind("]")
    if first_arr >= 0 and last_arr > first_arr:
        return text[first_arr:last_arr + 1]
    first_obj, last_obj = text.find("{"), text.rfind("}")
    if first_obj >= 0 and last_obj > first_obj:
        return text[first_obj:last_obj + 1]
    return text


class TooltipManager:
    """以滑鼠停留顯示說明，移開後立即消失。"""

    def __init__(self, root: tk.Misc, texts: Dict[str, str], delay_ms: int = 500):
        self.root = root
        self.texts = texts
        self.delay_ms = delay_ms
        self.after_id = None
        self.tip = None
        self.widget = None
        root.bind_class("TButton", "<Enter>", self._enter, add="+")
        root.bind_class("TButton", "<Leave>", self._leave, add="+")
        root.bind_class("TButton", "<ButtonPress>", self._leave, add="+")
        root.bind_class("TCheckbutton", "<Enter>", self._enter, add="+")
        root.bind_class("TCheckbutton", "<Leave>", self._leave, add="+")
        root.bind_class("TCombobox", "<Enter>", self._enter, add="+")
        root.bind_class("TCombobox", "<Leave>", self._leave, add="+")

    def _widget_text(self, widget: tk.Misc) -> str:
        try:
            text = str(widget.cget("text") or "").strip()
        except Exception:
            text = ""
        if not text:
            try:
                var_name = str(widget.cget("textvariable") or "")
                if var_name:
                    text = str(widget.getvar(var_name)).strip()
            except Exception:
                pass
        return text

    def _lookup(self, text: str) -> str:
        if text in self.texts:
            return self.texts[text]
        if text.startswith("無對應 A"):
            return self.texts.get("無對應 A", "")
        if text.startswith("無對應 B"):
            return self.texts.get("無對應 B", "")
        if text.startswith("配對衝突"):
            return self.texts.get("配對衝突", "")
        return ""

    def _enter(self, event):
        self._cancel()
        self.widget = event.widget
        text = self._lookup(self._widget_text(event.widget))
        if text:
            self.after_id = self.root.after(
                self.delay_ms, lambda w=event.widget, t=text: self._show(w, t)
            )

    def _leave(self, _event=None):
        self._cancel()
        self._hide()

    def _cancel(self):
        if self.after_id is not None:
            try:
                self.root.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None

    def _show(self, widget: tk.Misc, text: str):
        self.after_id = None
        try:
            if not widget.winfo_exists():
                return
            x = widget.winfo_rootx() + 12
            y = widget.winfo_rooty() + widget.winfo_height() + 6
        except Exception:
            return
        self._hide()
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except Exception:
            pass
        label = tk.Label(
            tip,
            text=text,
            justify="left",
            anchor="w",
            background="#ffffe0",
            foreground="black",
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=6,
            wraplength=330,
        )
        label.pack()
        tip.update_idletasks()

        screen_w = max(1, widget.winfo_screenwidth())
        screen_h = max(1, widget.winfo_screenheight())
        tip_w = tip.winfo_reqwidth()
        tip_h = tip.winfo_reqheight()
        margin = 8

        # 右側放不下時改放到按鈕左側；下方放不下時改放上方。
        if x + tip_w + margin > screen_w:
            x = widget.winfo_rootx() + widget.winfo_width() - tip_w
        if y + tip_h + margin > screen_h:
            y = widget.winfo_rooty() - tip_h - 6

        x = max(margin, min(x, screen_w - tip_w - margin))
        y = max(margin, min(y, screen_h - tip_h - margin))
        tip.geometry(f"+{x}+{y}")
        self.tip = tip

    def _hide(self):
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


class ConfigManager:
    def __init__(self) -> None:
        for folder in (CONFIG_DIR, TEMP_DIR, LOG_DIR):
            folder.mkdir(parents=True, exist_ok=True)
        try:
            test_file = APP_DIR / ".write_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
        except Exception as exc:
            raise RuntimeError(
                "程式所在資料夾無法寫入設定與暫存檔。\n"
                "請把整個程式資料夾移到桌面、文件、下載或其他可寫入位置，"
                "不要放在 Program Files。"
            ) from exc
        self.data: Dict[str, Any] = {
            "profiles": [],
            "active_profile": "",
            "window_geometry": "1450x820",
            "keep_unmatched_b": True,
            "keep_unmatched_a": True,
        }
        self.load()

    def load(self) -> None:
        if CONFIG_FILE.exists():
            try:
                loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except Exception:
                pass

    def save(self) -> None:
        CONFIG_FILE.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_key(self, profile_name: str) -> str:
        for p in self.data.get("profiles", []):
            if p.get("name") == profile_name:
                return p.get("api_key", "")
        return ""

    def set_key(self, profile_name: str, key: str) -> None:
        for p in self.data.get("profiles", []):
            if p.get("name") == profile_name:
                p["api_key"] = key
                return


PROVIDER_DEFAULTS = {
    "Gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-2.5-flash",
    },
    "OpenAI": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
    },
    "Anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-20250514",
    },
    "DeepSeek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
    },
    "OpenRouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "google/gemini-2.5-flash",
    },
    "OpenAI Compatible": {
        "base_url": "http://localhost:1234/v1",
        "model": "",
    },
    "Ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen3:8b",
    },
}


class AIClient:
    def __init__(self, profile: Dict[str, Any], api_key: str):
        if requests is None:
            raise RuntimeError("尚未安裝 requests。請執行：pip install -r requirements.txt")
        self.profile = profile
        self.api_key = api_key
        self.provider = profile.get("provider", "Gemini")
        self.base_url = profile.get("base_url", "").rstrip("/")
        self.model = profile.get("model", "")
        self.timeout = int(profile.get("timeout", 180))
        self.temperature = float(profile.get("temperature", 0.0))
        self.max_tokens = int(profile.get("max_tokens", 8192))

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        if self.provider == "Gemini":
            return self._gemini(system_prompt, user_prompt)
        if self.provider == "Anthropic":
            return self._anthropic(system_prompt, user_prompt)
        return self._openai_compatible(system_prompt, user_prompt)

    def _gemini(self, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("Gemini API Key 未設定。")
        url = f"{self.base_url}/models/{self.model}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
                "responseMimeType": "application/json",
            },
        }
        r = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        self._raise(r)
        data = r.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini 沒有回傳內容：{data}")
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)

    def _anthropic(self, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("Anthropic API Key 未設定。")
        url = f"{self.base_url}/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        r = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        self._raise(r)
        data = r.json()
        return "".join(x.get("text", "") for x in data.get("content", []) if x.get("type") == "text")

    def _openai_compatible(self, system_prompt: str, user_prompt: str) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.provider == "OpenRouter":
            headers["HTTP-Referer"] = self.profile.get("site_url", "") or "https://localhost"
            headers["X-Title"] = self.profile.get("site_name", "") or APP_NAME
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        r = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        self._raise(r)
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"]
        except Exception:
            raise RuntimeError(f"API 回傳格式無法解析：{data}")

    @staticmethod
    def _raise(response: Any) -> None:
        if response.ok:
            return
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise RuntimeError(f"HTTP {response.status_code}：{detail}")


class ProfileDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, config: ConfigManager):
        super().__init__(parent)
        self.title("AI API 與模型設定")
        self.geometry("760x560")
        self.transient(parent)
        self.grab_set()
        self.config_mgr = config
        self.profiles = [dict(p) for p in config.data.get("profiles", [])]
        self.current_index: Optional[int] = None

        self.name_var = tk.StringVar()
        self.provider_var = tk.StringVar(value="Gemini")
        self.key_var = tk.StringVar()
        self.base_var = tk.StringVar()
        self.model_var = tk.StringVar()
        self.temp_var = tk.StringVar(value="0")
        self.tokens_var = tk.StringVar(value="8192")
        self.timeout_var = tk.StringVar(value="180")
        self.site_name_var = tk.StringVar(value=APP_NAME)
        self.site_url_var = tk.StringVar()

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        left = ttk.Frame(outer)
        left.pack(side="left", fill="y", padx=(0, 10))
        right = ttk.Frame(outer)
        right.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="設定檔").pack(anchor="w")
        self.listbox = tk.Listbox(left, width=25, height=22)
        self.listbox.pack(fill="y", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.on_select)
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=5)
        ttk.Button(btns, text="新增", command=self.new_profile).pack(side="left")
        ttk.Button(btns, text="刪除", command=self.delete_profile).pack(side="left", padx=4)

        fields = [
            ("設定名稱", self.name_var),
            ("服務商", self.provider_var),
            ("API Key", self.key_var),
            ("Base URL", self.base_var),
            ("模型名稱", self.model_var),
            ("Temperature", self.temp_var),
            ("最大輸出 Token", self.tokens_var),
            ("Timeout 秒", self.timeout_var),
            ("OpenRouter 網站名稱", self.site_name_var),
            ("OpenRouter 網站網址", self.site_url_var),
        ]
        for row, (label, var) in enumerate(fields):
            ttk.Label(right, text=label).grid(row=row, column=0, sticky="w", pady=5)
            if label == "服務商":
                widget = ttk.Combobox(
                    right, textvariable=var, values=list(PROVIDER_DEFAULTS.keys()),
                    state="readonly", width=44
                )
                widget.bind("<<ComboboxSelected>>", self.provider_changed)
            else:
                widget = ttk.Entry(right, textvariable=var, width=47, show="*" if label == "API Key" else "")
            widget.grid(row=row, column=1, sticky="ew", pady=5)
        right.columnconfigure(1, weight=1)

        info = ttk.Label(
            right,
            text="模型名稱可直接手動輸入，不會被程式寫死。\n"
                 "先新增 AI 設定檔，選擇服務商並填入 API Key、模型與網址；可先按「測試」確認。\n"
                 "API Key 與模型設定會儲存在程式同資料夾的 config.json。分享程式時請勿一併分享該檔案。",
            foreground="#555",
            wraplength=450,
        )
        info.grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=10)

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="測試目前設定", command=self.test_profile).pack(side="left")
        ttk.Button(bottom, text="儲存", command=self.save_all).pack(side="right")
        ttk.Button(bottom, text="取消", command=self.destroy).pack(side="right", padx=5)

        self.refresh_list()
        if self.profiles:
            self.listbox.selection_set(0)
            self.on_select()

    def refresh_list(self):
        self.listbox.delete(0, "end")
        for p in self.profiles:
            self.listbox.insert("end", p.get("name", "(未命名)"))

    def commit_current(self):
        if self.current_index is None:
            return
        name = self.name_var.get().strip()
        if not name:
            return
        p = {
            "name": name,
            "provider": self.provider_var.get(),
            "api_key": self.key_var.get().strip(),
            "base_url": self.base_var.get().strip(),
            "model": self.model_var.get().strip(),
            "temperature": float(self.temp_var.get() or 0),
            "max_tokens": int(self.tokens_var.get() or 8192),
            "timeout": int(self.timeout_var.get() or 180),
            "site_name": self.site_name_var.get().strip(),
            "site_url": self.site_url_var.get().strip(),
        }
        self.profiles[self.current_index] = p

    def on_select(self, _event=None):
        if self.current_index is not None:
            try:
                self.commit_current()
            except Exception:
                pass
        sel = self.listbox.curselection()
        if not sel:
            return
        self.current_index = int(sel[0])
        p = self.profiles[self.current_index]
        self.name_var.set(p.get("name", ""))
        self.provider_var.set(p.get("provider", "Gemini"))
        self.base_var.set(p.get("base_url", ""))
        self.model_var.set(p.get("model", ""))
        self.temp_var.set(str(p.get("temperature", 0)))
        self.tokens_var.set(str(p.get("max_tokens", 8192)))
        self.timeout_var.set(str(p.get("timeout", 180)))
        self.site_name_var.set(p.get("site_name", APP_NAME))
        self.site_url_var.set(p.get("site_url", ""))
        self.key_var.set(p.get("api_key", self.config_mgr.get_key(p.get("name", ""))))

    def new_profile(self):
        if self.current_index is not None:
            self.commit_current()
        base_name = f"AI設定{len(self.profiles)+1}"
        defaults = PROVIDER_DEFAULTS["Gemini"]
        self.profiles.append({
            "name": base_name, "provider": "Gemini", "api_key": "",
            "base_url": defaults["base_url"], "model": defaults["model"],
            "temperature": 0, "max_tokens": 8192, "timeout": 180
        })
        self.refresh_list()
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(len(self.profiles)-1)
        self.current_index = None
        self.on_select()

    def delete_profile(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if messagebox.askyesno("刪除", "確定刪除此設定檔？", parent=self):
            del self.profiles[idx]
            self.current_index = None
            self.refresh_list()
            if self.profiles:
                self.listbox.selection_set(min(idx, len(self.profiles)-1))
                self.on_select()

    def provider_changed(self, _event=None):
        provider = self.provider_var.get()
        d = PROVIDER_DEFAULTS.get(provider, {})
        self.base_var.set(d.get("base_url", ""))
        self.model_var.set(d.get("model", ""))

    def get_current_profile(self) -> Tuple[Dict[str, Any], str]:
        name = self.name_var.get().strip() or "臨時設定"
        return ({
            "name": name,
            "provider": self.provider_var.get(),
            "base_url": self.base_var.get().strip(),
            "model": self.model_var.get().strip(),
            "temperature": float(self.temp_var.get() or 0),
            "max_tokens": int(self.tokens_var.get() or 8192),
            "timeout": int(self.timeout_var.get() or 180),
            "site_name": self.site_name_var.get().strip(),
            "site_url": self.site_url_var.get().strip(),
        }, self.key_var.get().strip())

    def test_profile(self):
        try:
            p, k = self.get_current_profile()
            result = AIClient(p, k).complete(
                "只回傳 JSON。",
                '請只回傳：{"ok":true}'
            )
            messagebox.showinfo("測試成功", result[:500], parent=self)
        except Exception as e:
            messagebox.showerror("測試失敗", str(e), parent=self)

    def save_all(self):
        try:
            if self.current_index is not None:
                self.commit_current()
            names = [p.get("name", "") for p in self.profiles]
            if any(not x for x in names) or len(names) != len(set(names)):
                raise ValueError("設定名稱不可空白或重複。")
            self.config_mgr.data["profiles"] = self.profiles
            if self.profiles and not self.config_mgr.data.get("active_profile"):
                self.config_mgr.data["active_profile"] = self.profiles[0]["name"]
            self.config_mgr.save()
            self.destroy()
        except Exception as e:
            messagebox.showerror("無法儲存", str(e), parent=self)


class SubtitleMatcherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.config_mgr = ConfigManager()
        self.title(f"字幕時間軸配對工具 v{APP_VERSION}")
        self.geometry(self.config_mgr.data.get("window_geometry", "1450x820"))
        self.minsize(1050, 620)

        self.a_entries: List[SrtEntry] = []
        self.b_entries: List[SrtEntry] = []
        self.a_path = ""
        self.b_path = ""
        self.matches: Dict[int, MatchInfo] = {}
        self.b_unique: set[int] = set()
        self.undo_stack: List[Tuple[List[SrtEntry], List[SrtEntry], Dict[int, MatchInfo], set[int]]] = []
        self.busy = False

        self.media_path = ""
        self.vlc_module = None
        self.vlc_instance = None
        self.vlc_player = None
        self.vlc_dir = ""
        self.vlc_dll_dir_handle = None
        self.media_duration_ms = 0
        self.media_position_ms = 0
        self.media_updating_slider = False
        self.preview_subtitle_path = ""
        self.preview_refresh_after_id = None
        self.find_text = ""
        self.find_side = "A"
        self.syncing_selection = False
        self.selection_sync_suspended = False
        self.selection_sync_after_id = None
        self.find_dialog = None
        self.find_query_var = None
        self.find_side_var = None
        self.overlap_warning_count = 0
        self.match_conflict_indices: List[int] = []
        self.match_conflict_cursor = -1
        self.unmatched_a_cursor = -1
        self.unmatched_b_cursor = -1

        self.unmatched_a_nav_var = tk.StringVar(value="無對應 A：0")
        self.unmatched_b_nav_var = tk.StringVar(value="無對應 B：0")
        self.conflict_nav_var = tk.StringVar(value="配對衝突：0 組")
        self.review_summary_var = tk.StringVar(value="待處理：無對應 A 0｜無對應 B 0｜配對衝突 0 組")

        self.keep_b_var = tk.BooleanVar(value=self.config_mgr.data.get("keep_unmatched_b", True))
        self.keep_a_var = tk.BooleanVar(value=self.config_mgr.data.get("keep_unmatched_a", True))
        self.autosave_after_id = None
        self.active_profile_var = tk.StringVar(value=self.config_mgr.data.get("active_profile", ""))
        self.status_var = tk.StringVar(value="請先載入 A、B 兩份 SRT。")
        self.media_time_var = tk.StringVar(value="00:00:00,000 / 00:00:00,000")
        self.media_name_var = tk.StringVar(value="尚未載入影片／音檔")

        self.ai_check_data: Dict[str, Any] = {}
        self.ai_report_window = None
        self.create_ui()
        self.tooltip_manager = TooltipManager(self, self.get_tooltip_texts())
        self.refresh_profiles()
        self.bind_all("<Return>", lambda e: self.apply_match_and_next())
        self.bind_all("<Control-z>", lambda e: self.undo())
        self.bind_all("<Control-s>", lambda e: self.export_srt())
        self.bind_all("<Control-f>", self.open_find_dialog)
        self.bind_all("<F3>", lambda e: self.find_next())
        self.bind_all("<Shift-F3>", lambda e: self.find_previous())
        self.bind_all("<Control-h>", self.open_replace_dialog)
        self.bind_all("<Escape>", lambda e: self.clear_all_selections())
        self.bind_all("<space>", self.on_space_key)
        self.bind_class("TButton", "<space>", self.on_space_key)
        self.bind_all("<Left>", self.on_left_key)
        self.bind_all("<Right>", self.on_right_key)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(300, self.offer_autosave_recovery)
        self.after(250, self.update_media_ui)

    def get_tooltip_texts(self) -> Dict[str, str]:
        return {
            "載入 A 人工字幕": "載入基準字幕。\n最後輸出會保留 A 的文字內容。",
            "載入 B AI 字幕": "載入目標字幕。\n最後輸出會使用 B 的時間軸。",
            "開啟專案": "載入之前儲存的字幕與配對結果。",
            "儲存專案": "將目前字幕、配對及編輯結果儲存為專案 JSON。",
            "載入影片／音檔": "載入影片或音檔，以便播放並核對字幕時間。",
            "快捷鍵一覽": "查看本程式可使用的鍵盤快捷鍵。",
            "操作說明": "開啟完整操作流程與功能說明。",
            "播放／暫停  Space": "播放或暫停目前影片／音檔。\n快捷鍵：Space。",
            "← 5 秒": "播放位置往前 5 秒。",
            "5 秒 →": "播放位置往後 5 秒。",
            "目前時間設為所選 A 開始": "將目前播放時間設為所選 A 字幕的開始時間。",
            "目前時間設為所選 A 結束": "將目前播放時間設為所選 A 字幕的結束時間。",
            "目前時間設為所選 B 開始": "將目前播放時間設為所選 B 字幕的開始時間。",
            "目前時間設為所選 B 結束": "將目前播放時間設為所選 B 字幕的結束時間。",
            "目前時間新增字幕到 A": "以目前播放時間在 A 新增一筆字幕。",
            "目前時間新增字幕到 B": "以目前播放時間在 B 新增一筆字幕。",
            "API／模型設定": "新增或修改 AI 供應商、模型、API Key 與網址。",
            "配對所選範圍": "只將目前選取的 A、B 範圍送給 AI 配對。\n適合局部補跑或修正。",
            "分段配對全部": "將全部字幕分段送給 AI 配對。\n每段完成後會立即匯入並自動儲存。",
            "建立配對並下一組  Enter": "建立目前所選 A、B 的配對，然後 A、B 同時移到下一筆。\n快捷鍵：Enter。",
            "解除配對": "解除目前所選 A 或 B 的配對關係。",
            "平分多A→單B": "將一筆 B 的時間平均分配給多筆 A。",
            "合併多A→單B": "將多筆 A 合併成一筆，套用同一個 B 的時間軸。\n建議完成全部配對後，全選 A 字幕，並取消 B 的選取，再使用此功能。",
            "取消選取 A": "清除目前 A 字幕的選取。",
            "取消選取 B": "清除目前 B 字幕的選取。",
            "新增 A": "新增一筆 A 字幕。",
            "編輯 A": "修改目前所選 A 的文字或時間。",
            "編輯 B": "修改目前所選 B 的文字或時間。",
            "刪除 A": "刪除目前所選 A 字幕。",
            "刪除 B": "刪除目前所選 B 字幕。",
            "合併所選 A": "將多筆所選 A 合併為一筆字幕。",
            "合併所選 B": "將多筆所選 B 合併為一筆字幕。",
            "復原 Ctrl+Z": "復原上一個配對或編輯操作。\n快捷鍵：Ctrl+Z。",
            "輸出 SRT": "輸出新的 SRT 字幕。\n使用 A 的文字與 B 的時間軸。",
            "無對應 A": "無對應為目前字幕沒有對應項目。\n按下可跳到下一筆無對應 A。",
            "無對應 B": "無對應為目前字幕沒有對應項目。\n按下可跳到下一筆無對應 B。",
            "配對衝突": "若出現類似：\nA1 → B1\nA2 → B1、B2\n需取消配對後重新手動配對。",
            "衝突清單": "列出全部配對衝突，並可直接跳到所選衝突。",
            "檢查時間重疊": "檢查輸出字幕是否有時間重疊。",
            "AI 檢查報告": "開啟 AI 配對檢查清單，可逐筆跳到需要人工確認的位置。",
            "上一個  Shift+F3": "跳到上一筆搜尋結果。",
            "下一個  F3": "跳到下一筆搜尋結果。",
            "關閉": "關閉目前視窗。",
            "確定": "套用目前設定或修改。",
            "取消": "取消並關閉目前視窗。",
            "儲存": "儲存目前設定。",
            "新增": "新增一個 AI 設定檔。",
            "刪除": "刪除目前選取的 AI 設定檔。",
            "測試目前設定": "呼叫一次 API，確認目前模型與金鑰能否正常使用。",
            "全部取代": "將符合條件的字幕文字全部取代。",
        }

    def _atomic_write_json(self, path: Path, data: Dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def save_ai_check(self) -> None:
        try:
            self._atomic_write_json(AI_CHECK_FILE, self.ai_check_data)
        except Exception as exc:
            self.set_status(f"AI 檢查紀錄儲存失敗：{exc}")

    def _new_ai_check(self, jobs, profile) -> None:
        self.ai_check_data = {
            "version": 1,
            "status": "running",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "provider": profile.get("provider", ""),
            "model": profile.get("model", ""),
            "total_chunks": len(jobs),
            "completed_chunks": 0,
            "sent_a_count": sum(len(a) for a, _ in jobs),
            "valid_returned_a": [],
            "explicit_unmatched_a": [],
            "unreturned_a": [],
            "invalid_results": [],
            "failed_chunks": [],
            "suspicious_matches": [],
        }
        self.save_ai_check()

    def _update_ai_check_chunk(self, chunk_no: int, report: Dict[str, Any]) -> None:
        data = self.ai_check_data
        data["completed_chunks"] = max(int(data.get("completed_chunks", 0)), chunk_no)
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        for key in ("valid_returned_a", "explicit_unmatched_a", "unreturned_a"):
            merged = set(data.get(key, []))
            merged.update(report.get(key, []))
            data[key] = sorted(merged)
        data.setdefault("invalid_results", []).extend(report.get("invalid_results", []))
        # 若某 A 後來局部補跑成功，從未回傳清單移除。
        valid = set(data.get("valid_returned_a", []))
        data["unreturned_a"] = [x for x in data.get("unreturned_a", []) if x not in valid]
        self.save_ai_check()

    def _entry_ref(self, side: str, index: int) -> Dict[str, Any]:
        entries = self.a_entries if side == "A" else self.b_entries
        if not (0 <= index < len(entries)):
            return {"side": side, "index": index}
        e = entries[index]
        return {
            "side": side,
            "uid": e.uid,
            "number": e.number,
            "start_ms": e.start_ms,
            "end_ms": e.end_ms,
            "text": e.text,
        }

    def _detect_suspicious_matches(self) -> List[Dict[str, Any]]:
        states = []
        for ai in range(len(self.a_entries)):
            m = self.matches.get(ai)
            states.append(bool(m and m.b_indices))

        suspicious: List[Dict[str, Any]] = []

        # 大片無對應之間僅有 1～3 筆配對：只標記，不自動取消。
        i = 0
        while i < len(states):
            if not states[i]:
                i += 1
                continue
            start = i
            while i + 1 < len(states) and states[i + 1]:
                i += 1
            end = i
            run_len = end - start + 1
            left_blank = 0
            j = start - 1
            while j >= 0 and not states[j] and left_blank < 20:
                left_blank += 1
                j -= 1
            right_blank = 0
            j = end + 1
            while j < len(states) and not states[j] and right_blank < 20:
                right_blank += 1
                j += 1
            if run_len <= 3 and left_blank >= 4 and right_blank >= 4:
                a_indices = list(range(start, end + 1))
                b_indices = sorted({
                    bi for ai in a_indices
                    for bi in self.matches.get(ai, MatchInfo(ai, [])).b_indices
                    if 0 <= bi < len(self.b_entries)
                })
                suspicious.append({
                    "type": "isolated_match",
                    "a_numbers": [self.a_entries[x].number for x in a_indices],
                    "a_refs": [self._entry_ref("A", x) for x in a_indices],
                    "b_refs": [self._entry_ref("B", x) for x in b_indices],
                    "reason": f"位於前後大片無對應之間（前 {left_blank} 筆、後 {right_blank} 筆）",
                    "review_status": "未檢查",
                })
            i += 1

        # B 編號倒退或突然大幅跳躍。
        sequence = []
        for ai, m in sorted(self.matches.items()):
            if m.b_indices:
                sequence.append((ai, min(m.b_indices), max(m.b_indices)))
        for prev, cur in zip(sequence, sequence[1:]):
            pai, _pmin, pmax = prev
            cai, cmin, _cmax = cur
            item = None
            if cmin < pmax - 2:
                item = {
                    "type": "b_order_backtrack",
                    "reason": "B 配對順序明顯倒退",
                }
            elif cmin - pmax > 80 and cai - pai <= 3:
                item = {
                    "type": "b_large_jump",
                    "reason": f"B 編號在相鄰 A 間突然跳躍 {cmin - pmax} 筆",
                }
            if item:
                a_indices = [pai, cai]
                b_indices = sorted({pmax, cmin})
                item.update({
                    "a_numbers": [self.a_entries[x].number for x in a_indices],
                    "a_refs": [self._entry_ref("A", x) for x in a_indices],
                    "b_refs": [self._entry_ref("B", x) for x in b_indices if 0 <= x < len(self.b_entries)],
                    "review_status": "未檢查",
                })
                suspicious.append(item)
        return suspicious

    def _load_ai_check_from_disk(self) -> Dict[str, Any]:
        if self.ai_check_data:
            return self.ai_check_data
        if AI_CHECK_FILE.exists():
            try:
                data = json.loads(AI_CHECK_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.ai_check_data = data
                    return data
            except Exception:
                pass
        return {}

    def _resolve_entry_ref(self, ref: Dict[str, Any]) -> Optional[int]:
        side = ref.get("side", "A")
        entries = self.a_entries if side == "A" else self.b_entries

        uid = str(ref.get("uid", ""))
        if uid:
            for i, entry in enumerate(entries):
                if getattr(entry, "uid", "") == uid:
                    return i

        # 舊紀錄沒有 UID 時，以時間與文字輔助定位。
        start_ms = ref.get("start_ms")
        text = str(ref.get("text", ""))
        candidates = []
        for i, entry in enumerate(entries):
            score = 0
            if start_ms is not None and abs(entry.start_ms - int(start_ms)) <= 1000:
                score += 3
            if text and entry.text == text:
                score += 4
            elif text and (text[:30] in entry.text or entry.text[:30] in text):
                score += 2
            if entry.number == ref.get("number"):
                score += 1
            if score:
                candidates.append((score, i))
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]

        number = ref.get("number")
        for i, entry in enumerate(entries):
            if entry.number == number:
                return i
        return None

    def _report_items(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []

        for item in data.get("suspicious_matches", []):
            row = dict(item)
            row["category"] = "疑似異常配對"
            row.setdefault("review_status", "未檢查")
            # 相容 v2.8：舊紀錄只有 A 編號。
            if not row.get("a_refs"):
                row["a_refs"] = [
                    {"side": "A", "number": n}
                    for n in row.get("a_numbers", [])
                ]
            items.append(row)

        missing = data.get("unreturned_a", [])
        if missing:
            for number in missing:
                items.append({
                    "category": "AI 未回傳",
                    "reason": "此 A 已送給 AI，但回傳中完全沒有出現。",
                    "a_numbers": [number],
                    "a_refs": [{"side": "A", "number": number}],
                    "b_refs": [],
                    "review_status": "未檢查",
                })

        for invalid in data.get("invalid_results", []):
            number = invalid.get("a")
            refs = [{"side": "A", "number": number}] if number is not None else []
            items.append({
                "category": "回傳格式異常",
                "reason": invalid.get("reason", "AI 回傳格式無法使用"),
                "a_numbers": [number] if number is not None else [],
                "a_refs": refs,
                "b_refs": [],
                "review_status": "未檢查",
            })

        for failed in data.get("failed_chunks", []):
            nums = failed.get("a_numbers", [])
            items.append({
                "category": "分段失敗",
                "reason": failed.get("reason", "此 AI 分段未完成"),
                "a_numbers": nums,
                "a_refs": [{"side": "A", "number": n} for n in nums[:3]],
                "b_refs": [],
                "review_status": "未檢查",
            })
        return items

    def _jump_report_item(self, item: Dict[str, Any], status_var=None):
        found_a = []
        found_b = []
        missing_labels = []

        for ref in item.get("a_refs", []):
            idx = self._resolve_entry_ref(ref)
            if idx is None:
                missing_labels.append(f"原 A{ref.get('number', '?')}")
            else:
                found_a.append(idx)

        for ref in item.get("b_refs", []):
            idx = self._resolve_entry_ref(ref)
            if idx is None:
                missing_labels.append(f"原 B{ref.get('number', '?')}")
            else:
                found_b.append(idx)

        self.selection_sync_suspended = True
        self.a_tree.selection_remove(*self.a_tree.selection())
        self.b_tree.selection_remove(*self.b_tree.selection())

        if found_a:
            ids = [f"a{i}" for i in sorted(set(found_a))]
            self.a_tree.selection_set(ids)
            self.a_tree.focus(ids[0])
            self.a_tree.see(ids[0])
        if found_b:
            ids = [f"b{i}" for i in sorted(set(found_b))]
            self.b_tree.selection_set(ids)
            self.b_tree.focus(ids[0])
            self.b_tree.see(ids[0])
        self.after(350, self._resume_selection_sync)

        if found_a or found_b:
            message = "已在主視窗選取相關字幕。"
            if missing_labels:
                message += " 找不到：" + "、".join(missing_labels)
            self.set_status(message)
        else:
            message = "此檢查項目的字幕已刪除、合併或找不到。"
            self.set_status(message)

        if status_var is not None:
            status_var.set(message)

    def show_ai_check_report(self):
        try:
            self._show_ai_check_report_impl()
        except Exception as exc:
            self.ai_report_window = None
            messagebox.showerror(
                "AI 檢查報告",
                f"無法開啟 AI 檢查報告：\n{exc}\n\n"
                f"偵錯紀錄：{AI_CHECK_FILE.name}",
                parent=self,
            )

    def _show_ai_check_report_impl(self):
        data = self._load_ai_check_from_disk()
        if not data:
            messagebox.showinfo("AI 檢查報告", "目前沒有 AI 檢查紀錄。", parent=self)
            return

        items = self._report_items(data)

        if self.ai_report_window is not None:
            try:
                if self.ai_report_window.winfo_exists():
                    self.ai_report_window.lift()
                    self.ai_report_window.focus_force()
                    return
            except Exception:
                self.ai_report_window = None

        dlg = tk.Toplevel(self)
        dlg.withdraw()
        self.ai_report_window = dlg
        dlg.title("AI 配對檢查報告")
        dlg.geometry("820x560")
        dlg.minsize(700, 440)
        dlg.protocol("WM_DELETE_WINDOW", lambda: (setattr(self, "ai_report_window", None), dlg.destroy()))

        header = ttk.Frame(dlg, padding=10)
        header.pack(fill="x")
        sent = int(data.get("sent_a_count", 0))
        unmatched = len(data.get("explicit_unmatched_a", []))
        missing = len(data.get("unreturned_a", []))
        invalid = len(data.get("invalid_results", []))
        suspicious = len(data.get("suspicious_matches", []))
        ttk.Label(
            header,
            text=(
                f"AI 已分析 {sent} 筆 A 字幕｜"
                f"明確判定無對應 {unmatched} 筆｜"
                f"未回傳 {missing} 筆｜"
                f"需要人工確認 {suspicious + invalid + len(data.get('failed_chunks', []))} 項"
            ),
            font=("", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            header,
            text="按「跳到這裡」後，本視窗不會關閉；可繼續檢查其他項目。",
        ).pack(anchor="w", pady=(4, 0))

        body = ttk.Frame(dlg, padding=(10, 0, 10, 8))
        body.pack(fill="both", expand=True)

        columns = ("status", "category", "a", "reason")
        tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="browse")
        tree.heading("status", text="狀態")
        tree.heading("category", text="類型")
        tree.heading("a", text="相關 A")
        tree.heading("reason", text="原因")
        tree.column("status", width=80, anchor="center")
        tree.column("category", width=120, anchor="center")
        tree.column("a", width=130, anchor="w")
        tree.column("reason", width=430, anchor="w")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for i, item in enumerate(items):
            nums = item.get("a_numbers", [])
            if nums:
                if len(nums) > 4:
                    a_text = f"A{nums[0]}～A{nums[-1]}"
                else:
                    a_text = "、".join(f"A{x}" for x in nums)
            else:
                a_text = "—"
            tree.insert(
                "", "end", iid=str(i),
                values=(
                    item.get("review_status", "未檢查"),
                    item.get("category", ""),
                    a_text,
                    item.get("reason", ""),
                )
            )

        detail_var = tk.StringVar(value="請選取一個項目。")
        ttk.Label(dlg, textvariable=detail_var, padding=(10, 2), wraplength=780).pack(fill="x")

        button_bar = ttk.Frame(dlg, padding=10)
        button_bar.pack(fill="x")
        cursor = {"index": 0}

        def selected_index():
            selected = tree.selection()
            if selected:
                return int(selected[0])
            return cursor["index"] if items else None

        event_guard = {"busy": False}

        def update_detail(index):
            if not items or index is None:
                return
            index %= len(items)
            cursor["index"] = index
            item = items[index]
            detail_var.set(
                f"{index + 1}/{len(items)}｜{item.get('category', '')}｜"
                f"{item.get('reason', '')}"
            )

        def select_index(index):
            if not items:
                return
            index %= len(items)
            event_guard["busy"] = True
            try:
                iid = str(index)
                if tree.selection() != (iid,):
                    tree.selection_set(iid)
                tree.focus(iid)
                tree.see(iid)
                update_detail(index)
            finally:
                event_guard["busy"] = False

        def jump_selected(_event=None):
            idx = selected_index()
            if idx is None:
                return
            cursor["index"] = idx
            self._jump_report_item(items[idx], detail_var)
            dlg.lift()

        def move(delta):
            idx = selected_index()
            if idx is None:
                return
            select_index(idx + delta)

        def mark(status):
            idx = selected_index()
            if idx is None:
                return
            items[idx]["review_status"] = status
            vals = list(tree.item(str(idx), "values"))
            vals[0] = status
            tree.item(str(idx), values=vals)
            detail_var.set(f"已標記為：{status}")
            # 同步回原始 suspicious_matches；其他臨時類型只保留在本次視窗。
            target = items[idx]
            for original in data.get("suspicious_matches", []):
                if original is target or (
                    original.get("a_numbers") == target.get("a_numbers")
                    and original.get("reason") == target.get("reason")
                ):
                    original["review_status"] = status
                    break
            self.ai_check_data = data
            self.save_ai_check()

        ttk.Button(button_bar, text="跳到這裡", command=jump_selected).pack(side="left")
        ttk.Button(button_bar, text="上一個", command=lambda: move(-1)).pack(side="left", padx=(8, 2))
        ttk.Button(button_bar, text="下一個", command=lambda: move(1)).pack(side="left", padx=2)
        ttk.Button(button_bar, text="確認正常", command=lambda: mark("確認正常")).pack(side="left", padx=(16, 2))
        ttk.Button(button_bar, text="已手動修正", command=lambda: mark("已手動修正")).pack(side="left", padx=2)
        ttk.Button(button_bar, text="忽略", command=lambda: mark("忽略")).pack(side="left", padx=2)
        ttk.Button(
            button_bar, text="關閉",
            command=lambda: (setattr(self, "ai_report_window", None), dlg.destroy())
        ).pack(side="right")

        def on_tree_select(_event=None):
            if event_guard["busy"]:
                return
            idx = selected_index()
            if idx is not None:
                update_detail(idx)

        tree.bind("<<TreeviewSelect>>", on_tree_select)
        tree.bind("<Double-1>", jump_selected)
        if items:
            select_index(0)
        else:
            detail_var.set("本次沒有需要人工確認的項目。AI 回傳完整且格式正常。")

        dlg.update_idletasks()
        dlg.deiconify()
        dlg.lift()
        dlg.focus_force()

    def show_ai_completion_dialog(self, total_chunks: int, total_applied: int):
        data = self.ai_check_data
        missing = len(data.get("unreturned_a", []))
        invalid = len(data.get("invalid_results", []))
        suspicious = len(data.get("suspicious_matches", []))
        failed = len(data.get("failed_chunks", []))
        unmatched = len(data.get("explicit_unmatched_a", []))
        review_count = missing + invalid + suspicious + failed

        dlg = tk.Toplevel(self)
        dlg.title("AI 配對完成")
        dlg.resizable(False, False)
        dlg.transient(self)

        frame = ttk.Frame(dlg, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="AI 配對完成", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                f"✓ 已完成 {total_chunks} 個分段\n"
                f"✓ 共分析 {data.get('sent_a_count', 0)} 筆 A 字幕\n"
                f"✓ 已套用 {total_applied} 筆配對結果\n"
                f"{'✓ AI 全部都有回傳' if missing == 0 else f'⚠ AI 未回傳 {missing} 筆'}\n\n"
                f"AI 明確判定沒有對應：{unmatched} 筆\n"
                f"需要人工確認：{review_count} 項"
            ),
            justify="left",
        ).pack(anchor="w", pady=(10, 12))

        if review_count:
            ttk.Label(
                frame,
                text=(
                    "建議開啟檢查清單，逐筆跳到主視窗確認。\n\n"
                    "偵錯紀錄：ai_check_result.json\n"
                    "（一般使用者不用開啟）"
                ),
                wraplength=430,
            ).pack(anchor="w")
        else:
            ttk.Label(
                frame,
                text="本次 AI 回傳完整，且沒有偵測到格式或配對順序異常。",
                wraplength=430,
            ).pack(anchor="w")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(16, 0))

        def open_report():
            dlg.destroy()
            try:
                self.show_ai_check_report()
            except Exception as exc:
                try:
                    self.ai_report_window = None
                except Exception:
                    pass
                messagebox.showerror(
                    "AI 檢查報告",
                    f"無法開啟 AI 檢查報告：\n{exc}\n\n"
                    f"偵錯紀錄：{AI_CHECK_FILE.name}",
                    parent=self,
                )

        ttk.Button(buttons, text="開啟檢查", command=open_report).pack(side="left")
        ttk.Button(buttons, text="稍後檢查", command=dlg.destroy).pack(side="right")

        dlg.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dlg.winfo_reqwidth()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dlg.winfo_reqheight()) // 2)
        dlg.geometry(f"+{x}+{y}")
        dlg.lift()
        dlg.focus_force()


    def create_ui(self):
        toolbar = ttk.Frame(self, padding=8)
        toolbar.pack(fill="x")

        ttk.Button(toolbar, text="載入 A 人工字幕", command=self.load_a).pack(side="left")
        self.a_label = ttk.Label(toolbar, text="尚未載入", width=28)
        self.a_label.pack(side="left", padx=(4, 16))

        ttk.Button(toolbar, text="載入 B AI 字幕", command=self.load_b).pack(side="left")
        self.b_label = ttk.Label(toolbar, text="尚未載入", width=28)
        self.b_label.pack(side="left", padx=(4, 16))

        ttk.Button(toolbar, text="開啟專案", command=self.load_project).pack(side="left")
        ttk.Button(toolbar, text="儲存專案", command=self.save_project).pack(side="left", padx=4)
        ttk.Button(toolbar, text="載入影片／音檔", command=self.load_media).pack(side="left", padx=(18, 4))
        ttk.Button(toolbar, text="快捷鍵一覽", command=self.show_shortcuts).pack(side="left", padx=4)
        ttk.Button(toolbar, text="操作說明", command=self.show_operation_guide).pack(side="left", padx=4)
        ttk.Label(toolbar, textvariable=self.media_name_var, width=32).pack(side="left")

        # 使用原生 tk.PanedWindow，分隔線較明顯、可確實拖曳。
        self.main_vertical_paned = tk.PanedWindow(
            self,
            orient="vertical",
            sashwidth=12,
            sashrelief="raised",
            showhandle=True,
            opaqueresize=True,
            background="#b7b7b7",
            borderwidth=0,
        )
        self.main_vertical_paned.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        media_container = ttk.Frame(self.main_vertical_paned)
        workspace = ttk.Frame(self.main_vertical_paned)
        self.main_vertical_paned.add(media_container, minsize=180, stretch="always")
        self.main_vertical_paned.add(workspace, minsize=260, stretch="always")
        self.after_idle(lambda: self._set_initial_vertical_sash())

        media_frame = ttk.LabelFrame(media_container, text="影片／音檔播放確認（拖曳下方分隔線可調整高度）", padding=6)
        media_frame.pack(fill="both", expand=True)

        self.video_canvas = tk.Canvas(media_frame, height=280, background="black", highlightthickness=0)
        self.video_canvas.pack(side="left", fill="both", expand=True)
        self.video_placeholder = self.video_canvas.create_text(
            20, 20, anchor="nw", fill="white",
            text="尚未載入影片／音檔"
        )

        media_controls = ttk.Frame(media_frame, padding=(8, 0, 0, 0))
        media_controls.pack(side="left", fill="y")

        ttk.Label(media_controls, textvariable=self.media_time_var, font=("", 11)).pack(anchor="center", pady=(0, 6))
        ttk.Button(media_controls, text="播放／暫停  Space", command=self.toggle_play_pause).pack(fill="x")
        seek_row = ttk.Frame(media_controls)
        seek_row.pack(fill="x", pady=5)
        ttk.Button(seek_row, text="← 5 秒", command=lambda: self.seek_relative(-5000)).pack(side="left", fill="x", expand=True)
        ttk.Button(seek_row, text="5 秒 →", command=lambda: self.seek_relative(5000)).pack(side="left", fill="x", expand=True, padx=(4, 0))

        ttk.Separator(media_controls).pack(fill="x", pady=6)
        ttk.Button(media_controls, text="目前時間設為所選 A 開始", command=lambda: self.set_selected_start_from_media("A")).pack(fill="x", pady=2)
        ttk.Button(media_controls, text="目前時間設為所選 A 結束", command=lambda: self.set_selected_end_from_media("A")).pack(fill="x", pady=2)
        ttk.Button(media_controls, text="目前時間設為所選 B 開始", command=lambda: self.set_selected_start_from_media("B")).pack(fill="x", pady=(6, 2))
        ttk.Button(media_controls, text="目前時間設為所選 B 結束", command=lambda: self.set_selected_end_from_media("B")).pack(fill="x", pady=2)
        ttk.Button(media_controls, text="目前時間新增字幕到 A", command=lambda: self.add_subtitle_at_media("A")).pack(fill="x", pady=(8, 2))
        ttk.Button(media_controls, text="目前時間新增字幕到 B", command=lambda: self.add_subtitle_at_media("B")).pack(fill="x", pady=2)

        slider_row = ttk.Frame(media_container, padding=(0, 4, 0, 0))
        slider_row.pack(fill="x")
        self.media_slider = ttk.Scale(slider_row, from_=0, to=1000, orient="horizontal", command=self.on_media_slider)
        self.media_slider.pack(fill="x", expand=True)

        controls = ttk.Frame(workspace, padding=(8, 0, 8, 6))
        controls.pack(fill="x")
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        ai_group = ttk.LabelFrame(controls, text="AI", padding=5)
        ai_group.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))
        ttk.Label(ai_group, text="設定：").pack(side="left")
        self.profile_combo = ttk.Combobox(ai_group, textvariable=self.active_profile_var, width=22, state="readonly")
        self.profile_combo.pack(side="left")
        self.profile_combo.bind("<<ComboboxSelected>>", self.profile_selected)
        ttk.Button(ai_group, text="API／模型設定", command=self.open_profiles).pack(side="left", padx=4)
        ttk.Button(ai_group, text="配對所選範圍", command=self.ai_match_selected).pack(side="left", padx=(8, 3))
        ttk.Button(ai_group, text="分段配對全部", command=self.ai_match_all).pack(side="left")

        match_group = ttk.LabelFrame(controls, text="配對", padding=5)
        match_group.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 4))
        ttk.Button(match_group, text="建立配對並下一組  Enter", command=self.apply_match_and_next).pack(side="left")
        ttk.Button(match_group, text="解除配對", command=self.set_unmatched_or_unlink).pack(side="left", padx=4)
        ttk.Button(match_group, text="平分多A→單B", command=lambda: self.split_selected_a_over_b("equal")).pack(side="left", padx=(8, 4))
        ttk.Button(match_group, text="合併多A→單B", command=self.merge_many_a_to_one_b_in_selection).pack(side="left")
        ttk.Button(match_group, text="取消選取 A", command=lambda: self.clear_tree_selection(self.a_tree)).pack(side="left", padx=(8, 3))
        ttk.Button(match_group, text="取消選取 B", command=lambda: self.clear_tree_selection(self.b_tree)).pack(side="left")

        subtitle_group = ttk.LabelFrame(controls, text="字幕", padding=5)
        subtitle_group.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Button(subtitle_group, text="新增 A", command=self.add_a_subtitle).pack(side="left")
        ttk.Button(subtitle_group, text="編輯 A", command=self.edit_selected_a).pack(side="left", padx=3)
        ttk.Button(subtitle_group, text="編輯 B", command=self.edit_selected_b).pack(side="left", padx=3)
        ttk.Button(subtitle_group, text="刪除 A", command=self.delete_selected_a).pack(side="left", padx=(8, 3))
        ttk.Button(subtitle_group, text="刪除 B", command=self.delete_selected_b).pack(side="left", padx=3)
        ttk.Button(subtitle_group, text="合併所選 A", command=lambda: self.merge_selected_subtitles("A")).pack(side="left", padx=(8, 3))
        ttk.Button(subtitle_group, text="合併所選 B", command=lambda: self.merge_selected_subtitles("B")).pack(side="left", padx=3)
        ttk.Button(subtitle_group, text="復原 Ctrl+Z", command=self.undo).pack(side="left", padx=(8, 3))
        ttk.Button(subtitle_group, text="輸出 SRT", command=self.export_srt).pack(side="left", padx=(8, 0))

        options_bar = ttk.Frame(workspace, padding=(8, 0, 8, 5))
        options_bar.pack(fill="x")
        ttk.Checkbutton(options_bar, text="輸出時保留 B 未配對字幕", variable=self.keep_b_var).pack(side="right")
        ttk.Checkbutton(options_bar, text="輸出時保留 A 未配對字幕（使用手動時間）", variable=self.keep_a_var).pack(side="right", padx=(0, 12))

        nav_bar = ttk.Frame(workspace, padding=(8, 0, 8, 6))
        nav_bar.pack(fill="x")
        ttk.Label(nav_bar, text="快速檢查：").pack(side="left")
        ttk.Button(nav_bar, text="←", width=3, command=lambda: self.jump_unmatched("A", -1)).pack(side="left", padx=(4, 1))
        ttk.Button(nav_bar, textvariable=self.unmatched_a_nav_var, command=lambda: self.jump_unmatched("A", 1)).pack(side="left", padx=1)
        ttk.Button(nav_bar, text="→", width=3, command=lambda: self.jump_unmatched("A", 1)).pack(side="left", padx=(1, 8))
        ttk.Button(nav_bar, text="←", width=3, command=lambda: self.jump_unmatched("B", -1)).pack(side="left", padx=(4, 1))
        ttk.Button(nav_bar, textvariable=self.unmatched_b_nav_var, command=lambda: self.jump_unmatched("B", 1)).pack(side="left", padx=1)
        ttk.Button(nav_bar, text="→", width=3, command=lambda: self.jump_unmatched("B", 1)).pack(side="left", padx=(1, 8))
        ttk.Button(nav_bar, text="←", width=3, command=lambda: self.jump_match_conflict(-1)).pack(side="left", padx=(4, 1))
        ttk.Button(nav_bar, textvariable=self.conflict_nav_var, command=lambda: self.jump_match_conflict(1)).pack(side="left", padx=1)
        ttk.Button(nav_bar, text="→", width=3, command=lambda: self.jump_match_conflict(1)).pack(side="left", padx=(1, 6))
        ttk.Button(nav_bar, text="衝突清單", command=self.show_match_conflict_report).pack(side="left", padx=4)
        ttk.Button(nav_bar, text="檢查時間重疊", command=self.show_overlap_report).pack(side="left", padx=4)
        ttk.Button(nav_bar, text="AI 檢查報告", command=self.show_ai_check_report).pack(side="left", padx=4)
        ttk.Label(nav_bar, textvariable=self.review_summary_var).pack(side="right")

        paned = tk.PanedWindow(
            workspace,
            orient="horizontal",
            sashwidth=10,
            sashrelief="raised",
            showhandle=True,
            opaqueresize=True,
            background="#b7b7b7",
            borderwidth=0,
        )
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        left_frame = ttk.LabelFrame(paned, text="A：人工校正中文字幕（保留文字）", padding=4)
        right_frame = ttk.LabelFrame(paned, text="B：AI 中文字幕（採用時間軸）", padding=4)
        paned.add(left_frame, minsize=300, stretch="always")
        paned.add(right_frame, minsize=300, stretch="always")

        cols = ("num", "start", "end", "text", "match")
        self.a_tree = ttk.Treeview(left_frame, columns=cols, show="headings", selectmode="extended")
        self.b_tree = ttk.Treeview(right_frame, columns=cols, show="headings", selectmode="extended")
        for tree in (self.a_tree, self.b_tree):
            tree.heading("num", text="編號")
            tree.heading("start", text="開始")
            tree.heading("end", text="結束")
            tree.heading("text", text="字幕")
            tree.heading("match", text="配對")
            tree.column("num", width=60, stretch=False, anchor="center")
            tree.column("start", width=105, stretch=False)
            tree.column("end", width=105, stretch=False)
            tree.column("text", width=400)
            tree.column("match", width=120, stretch=False)
            tree.tag_configure("matched", background="#93c47d")
            tree.tag_configure("ai", background="#6fa8dc")
            tree.tag_configure("unmatched", background="#e6b8af")
            tree.tag_configure("unique", background="#ffd966")

        self._pack_tree(left_frame, self.a_tree)
        self._pack_tree(right_frame, self.b_tree)
        self.a_tree.bind("<<TreeviewSelect>>", self.sync_b_from_a)
        self.b_tree.bind("<<TreeviewSelect>>", self.sync_a_from_b)
        self.a_tree.bind("<Button-1>", lambda e: self.on_tree_mouse_press(self.a_tree, "A", e), add="+")
        self.b_tree.bind("<Button-1>", lambda e: self.on_tree_mouse_press(self.b_tree, "B", e), add="+")
        self.a_tree.bind("<Double-1>", lambda e: self.jump_from_tree_double_click(self.a_tree, "A", e), add="+")
        self.b_tree.bind("<Double-1>", lambda e: self.jump_from_tree_double_click(self.b_tree, "B", e), add="+")

        detail = ttk.LabelFrame(workspace, text="目前選取內容", padding=6)
        detail.pack(fill="x", padx=8, pady=(0, 6))
        self.detail_text = tk.Text(detail, height=5, wrap="word")
        self.detail_text.pack(fill="x")
        self.detail_text.configure(state="disabled")

        status = ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=4)
        status.pack(fill="x", side="bottom")

    @staticmethod
    def _pack_tree(frame: ttk.Frame, tree: ttk.Treeview):
        y = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        x = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    def _set_initial_vertical_sash(self):
        """第一次顯示時把影片區設為約 42%，之後可自由拖曳粗分隔線。"""
        try:
            total = max(500, self.main_vertical_paned.winfo_height())
            self.main_vertical_paned.sash_place(0, 0, int(total * 0.42))
        except Exception:
            pass

    def normalize_legacy_project_state(self):
        """相容 v1.8 等舊專案：移除已停用的無對應狀態並修正常見欄位。"""
        if not hasattr(self, "matches") or self.matches is None:
            self.matches = {}
        normalized = {}
        for raw_ai, raw_match in list(self.matches.items()):
            try:
                ai = int(raw_ai)
            except Exception:
                continue
            if not (0 <= ai < len(self.a_entries)):
                continue
            try:
                raw_b = getattr(raw_match, "b_indices", [])
                valid_b = sorted({
                    int(bi) for bi in raw_b
                    if str(bi).lstrip("-").isdigit() and 0 <= int(bi) < len(self.b_entries)
                })
                raw_match.a_index = ai
                raw_match.b_indices = valid_b
                if valid_b:
                    normalized[ai] = raw_match
            except Exception:
                continue
        self.matches = normalized
        self.b_unique = set()

    def refresh_profiles(self):
        profiles = self.config_mgr.data.get("profiles", [])
        names = [p.get("name", "") for p in profiles]
        self.profile_combo["values"] = names
        if names:
            if self.active_profile_var.get() not in names:
                self.active_profile_var.set(names[0])
        else:
            self.active_profile_var.set("")

    def profile_selected(self, _event=None):
        self.config_mgr.data["active_profile"] = self.active_profile_var.get()
        self.config_mgr.save()

    def open_profiles(self):
        dlg = ProfileDialog(self, self.config_mgr)
        self._protect_dialog_undo(dlg)
        self.wait_window(dlg)
        self.config_mgr.load()
        self.refresh_profiles()

    def is_text_input_focused(self) -> bool:
        widget = self.focus_get()
        return isinstance(widget, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox, tk.Spinbox))

    def on_space_key(self, event=None):
        if self.is_text_input_focused():
            return None
        self.toggle_play_pause()
        return "break"

    def on_left_key(self, event=None):
        if self.is_text_input_focused():
            return None
        self.seek_relative(-5000)
        return "break"

    def on_right_key(self, event=None):
        if self.is_text_input_focused():
            return None
        self.seek_relative(5000)
        return "break"

    def show_shortcuts(self):
        messagebox.showinfo(
            "快捷鍵一覽",
            "播放器\n"
            "Space：播放／暫停\n"
            "←：後退 5 秒\n"
            "→：前進 5 秒\n"
            "雙擊 A 或 B 字幕：跳到該字幕開始時間並繼續播放\n\n"
            "字幕與專案\n"
            "Enter：建立配對，A、B 同時跳到各自下一條\n"
            "Ctrl+Z：復原\n"
            "Ctrl+S：匯出 SRT\n"
            "Ctrl+F：開啟搜尋（可指定 A／B／A+B）\n"
            "F3：下一個搜尋結果\n"
            "Shift+F3：上一個搜尋結果\n"
            "Ctrl+H：搜尋並取代\n"
            "Ctrl+滑鼠點擊：保留另一側選取並加選字幕\n"
            "Shift+滑鼠點擊：同一側連續範圍選取\n"
            "配對衝突 ←／→：逐組跳到容易造成重疊的配對\n"
            "Esc：取消 A、B 選取\n\n"
            "焦點在文字輸入框時，Space 與方向鍵維持一般輸入功能。"
        )

    def show_operation_guide(self):
        messagebox.showinfo(
            "操作說明",
            "用途\n"
            "以已校對完成的字幕 A 為文字基礎，利用 AI 比對另一個影片／版本的字幕 B，"
            "最後輸出 A 的文字＋B 的時間軸。AI 結果仍需人工確認。\n\n"
            "檔案要求\n"
            "• A：已校對完成、希望保留文字的字幕。\n"
            "• B：另一個影片或版本的字幕；時間軸應大致正確，文字可尚未與 A 一致。\n"
            "• A、B 目前只接受 SRT 格式。\n"
            "• 影片／音檔為選用，可載入後同步確認畫面與時間。\n\n"
            "建議流程\n"
            "1. 載入 A 與 B 字幕。\n"
            "2. 第一次使用先進入「API／模型設定」，新增設定檔、填入 API Key 並測試。\n"
            "3. 執行 AI 配對；會依目前設定自動分段送給 AI。\n"
            "4. 開啟「AI 檢查報告」，依序檢查 AI 未回傳、疑似異常配對等項目。\n"
            "5. 處理「配對衝突」、處理「無對應 A」再處理「無對應 B」。\n"
            "5. 刪除不需要保留的 A 或 B 字幕，並人工修正錯誤配對。\n"
            "6. 全選 A 字幕，並取消 B 的選取，再使用「合併多A→單B」\n"
            "7. 輸出 SRT，再到專業字幕軟體進行最後檢查與微調。\n\n"
            "資料位置\n"
            "config.json、autosave_project.json、temp 與 logs 都存放在程式同一資料夾。\n"
            "請勿把含 API Key 的 config.json 分享給其他人。"
        )

    def application_dirs(self):
        script_dir = Path(__file__).resolve().parent
        exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else script_dir
        bundle_dir = Path(getattr(sys, "_MEIPASS", exe_dir))
        result = []
        for directory in (exe_dir, script_dir, bundle_dir):
            if directory not in result:
                result.append(directory)
        return result

    def find_vlc_dir(self):
        """尋找可攜 VLC 或 Windows 已安裝的官方 VLC。"""
        candidates = []
        for base in self.application_dirs():
            candidates.extend([base / "vlc", base / "VLC", base])

        for env_name in ("VLC_HOME", "VLC_PATH"):
            value = os.environ.get(env_name, "").strip().strip('"')
            if value:
                candidates.append(Path(value))

        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            value = os.environ.get(env_name, "").strip()
            if value:
                candidates.append(Path(value) / "VideoLAN" / "VLC")

        for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
            raw_dir = raw_dir.strip().strip('"')
            if raw_dir:
                candidates.append(Path(raw_dir))

        checked = set()
        for folder in candidates:
            try:
                folder = folder.resolve()
            except Exception:
                pass
            key = str(folder).lower()
            if key in checked:
                continue
            checked.add(key)
            if (folder / "libvlc.dll").is_file() and (folder / "libvlccore.dll").is_file():
                return str(folder)
        return ""

    def prepare_vlc(self) -> bool:
        vlc_dir = self.find_vlc_dir()
        if not vlc_dir:
            open_site = messagebox.askyesno(
                "缺少 VLC",
                "找不到 VLC 播放核心。\n\n"
                "請安裝官方 VLC 64 位元，或把官方 VLC ZIP 完整解壓到程式旁的 vlc 資料夾。\n\n"
                "注意：可攜版不能只複製 libvlc.dll，必須保留 libvlccore.dll 與 plugins 資料夾。\n\n"
                "要開啟 VLC 官方下載頁嗎？"
            )
            if open_site:
                import webbrowser
                webbrowser.open("https://www.videolan.org/vlc/download-windows.html")
            return False

        self.vlc_dir = vlc_dir
        plugins_dir = str(Path(vlc_dir) / "plugins")
        os.environ["VLC_PLUGIN_PATH"] = plugins_dir
        os.environ["PATH"] = vlc_dir + os.pathsep + os.environ.get("PATH", "")

        if os.name == "nt" and hasattr(os, "add_dll_directory"):
            try:
                self.vlc_dll_dir_handle = os.add_dll_directory(vlc_dir)
            except OSError:
                pass

        self.set_status(f"已找到 VLC：{vlc_dir}")
        return True

    def import_python_vlc(self) -> bool:
        if self.vlc_module is not None:
            return True
        if not self.prepare_vlc():
            return False
        try:
            self.vlc_module = importlib.import_module("vlc")
            return True
        except ImportError:
            install = messagebox.askyesno(
                "缺少 Python VLC 控制套件",
                "尚未安裝 python-vlc。\n\n"
                "要現在自動執行下列命令嗎？\n"
                f'"{sys.executable}" -m pip install python-vlc\n\n'
                "需要網路連線。"
            )
            if not install:
                return False
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "python-vlc"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=180,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or result.stdout.strip())
                importlib.invalidate_caches()
                self.vlc_module = importlib.import_module("vlc")
                messagebox.showinfo("安裝完成", "python-vlc 已安裝完成。")
                return True
            except Exception as exc:
                messagebox.showerror(
                    "自動安裝失敗",
                    f"無法自動安裝 python-vlc：\n{exc}\n\n"
                    "可自行在命令提示字元執行：\n"
                    "py -m pip install python-vlc"
                )
                return False
        except Exception as exc:
            messagebox.showerror(
                "VLC 載入失敗",
                f"找到 VLC，但無法載入 libVLC：\n{exc}\n\n"
                "請確認 VLC 與 Python／EXE 的位元數相同，且 VLC 資料夾完整包含 plugins。"
            )
            return False

    def ensure_vlc_player(self) -> bool:
        if self.vlc_player is not None:
            return True
        if not self.import_python_vlc():
            return False
        try:
            self.update_idletasks()
            self.vlc_instance = self.vlc_module.Instance("--no-video-title-show", "--quiet")
            self.vlc_player = self.vlc_instance.media_player_new()
            window_id = int(self.video_canvas.winfo_id())
            if sys.platform.startswith("win"):
                self.vlc_player.set_hwnd(window_id)
            elif sys.platform == "darwin":
                self.vlc_player.set_nsobject(window_id)
            else:
                self.vlc_player.set_xwindow(window_id)
            self.video_canvas.delete(self.video_placeholder)
            return True
        except Exception as exc:
            self.vlc_player = None
            messagebox.showerror("播放器建立失敗", f"無法把 VLC 嵌入影片區域：\n{exc}")
            return False

    def load_media(self):
        path = filedialog.askopenfilename(
            title="選擇影片或音檔",
            filetypes=[
                ("影片／音檔", "*.mp4 *.mkv *.avi *.mov *.webm *.mp3 *.wav *.flac *.m4a *.aac *.ogg"),
                ("所有檔案", "*.*"),
            ],
        )
        if not path:
            return
        if not self.ensure_vlc_player():
            return
        try:
            self.media_path = path
            self.media_name_var.set(Path(path).name)
            self.media_position_ms = 0
            self.media_duration_ms = 0
            media = self.vlc_instance.media_new(path)
            self.vlc_player.set_media(media)
            self.vlc_player.play()
            self.media_slider.set(0)
            self.schedule_autosave()
            self.set_status(f"已從頭播放：{Path(path).name}")
            self.after(800, self.refresh_preview_subtitle)
        except Exception as exc:
            messagebox.showerror("載入媒體失敗", str(exc))

    def toggle_play_pause(self):
        if not self.media_path:
            self.load_media()
            return
        if not self.ensure_vlc_player():
            return
        try:
            if self.vlc_player.is_playing():
                self.vlc_player.pause()
                self.set_status("已暫停")
            else:
                self.vlc_player.play()
                self.set_status("播放中")
        except Exception as exc:
            messagebox.showerror("播放控制失敗", str(exc))

    def current_media_time(self) -> int:
        if self.vlc_player is None:
            return max(0, int(self.media_position_ms))
        try:
            value = self.vlc_player.get_time()
            if value is not None and value >= 0:
                self.media_position_ms = int(value)
        except Exception:
            pass
        return self.media_position_ms

    def seek_to(self, target_ms: int, autoplay: bool = True):
        if not self.media_path or not self.ensure_vlc_player():
            return
        target_ms = max(0, int(target_ms))
        if self.media_duration_ms > 0:
            target_ms = min(target_ms, self.media_duration_ms)
        try:
            # VLC 有時需要先進入播放狀態，set_time 才會立即生效。
            if autoplay and not self.vlc_player.is_playing():
                self.vlc_player.play()
            self.vlc_player.set_time(target_ms)
            self.media_position_ms = target_ms
            if autoplay:
                self.vlc_player.play()
            self.update_media_ui_once()
            self.set_status(f"已跳到並播放：{ms_to_srt(target_ms)}")
        except Exception as exc:
            messagebox.showerror("跳轉失敗", str(exc))

    def seek_relative(self, delta_ms: int):
        if not self.media_path:
            return
        self.seek_to(self.current_media_time() + delta_ms, autoplay=True)

    def update_media_ui_once(self):
        current = self.current_media_time()
        if self.vlc_player is not None:
            try:
                duration = self.vlc_player.get_length()
                if duration is not None and duration > 0:
                    self.media_duration_ms = int(duration)
            except Exception:
                pass
        self.media_time_var.set(f"{ms_to_srt(current)} / {ms_to_srt(self.media_duration_ms)}")
        if self.media_duration_ms > 0:
            self.media_updating_slider = True
            self.media_slider.set(current / self.media_duration_ms * 1000)
            self.media_updating_slider = False

    def update_media_ui(self):
        try:
            self.update_media_ui_once()
        finally:
            self.after(200, self.update_media_ui)

    def on_media_slider(self, value):
        if self.media_updating_slider or not self.media_path or self.media_duration_ms <= 0:
            return
        try:
            target = float(value) / 1000.0 * self.media_duration_ms
            self.seek_to(int(target), autoplay=True)
        except Exception:
            pass

    def jump_from_tree_double_click(self, tree: ttk.Treeview, side: str, event):
        row = tree.identify_row(event.y)
        if not row or not self.media_path:
            return
        try:
            index = int(row[1:])
            entry = self.a_entries[index] if side == "A" else self.b_entries[index]
            self.seek_to(entry.start_ms, autoplay=True)
        except Exception:
            pass

    def set_selected_start_from_media(self, side: str):
        if not self.media_path:
            messagebox.showwarning("尚未載入媒體", "請先載入影片或音檔。")
            return
        current = self.current_media_time()
        tree = self.a_tree if side == "A" else self.b_tree
        entries = self.a_entries if side == "A" else self.b_entries
        prefix = "a" if side == "A" else "b"
        selected = self.selected_indices(tree, prefix)
        if not selected:
            messagebox.showwarning("尚未選取", f"請先選取一條 {side} 字幕。")
            return
        self.snapshot()
        for idx in selected:
            entry = entries[idx]
            duration = max(300, entry.end_ms - entry.start_ms)
            entry.start_ms = current
            entry.end_ms = current + duration
            if side == "A":
                m = self.matches.get(idx)
                if m and not m.b_indices:
                    m.assigned_start_ms = entry.start_ms
                    m.assigned_end_ms = entry.end_ms
                    m.status = "manual_a"
        self.refresh_trees()
        tree.selection_set([f"{prefix}{i}" for i in selected])
        tree.see(f"{prefix}{selected[0]}")
        self.schedule_autosave()
        self.set_status(f"已將目前播放時間設為所選 {side} 字幕開始。")

    def set_selected_end_from_media(self, side: str):
        if not self.media_path:
            messagebox.showwarning("尚未載入媒體", "請先載入影片或音檔。")
            return
        current = self.current_media_time()
        tree = self.a_tree if side == "A" else self.b_tree
        entries = self.a_entries if side == "A" else self.b_entries
        prefix = "a" if side == "A" else "b"
        selected = self.selected_indices(tree, prefix)
        if not selected:
            messagebox.showwarning("尚未選取", f"請先選取一條 {side} 字幕。")
            return
        invalid = [idx for idx in selected if current <= entries[idx].start_ms]
        if invalid:
            messagebox.showwarning(
                "時間不正確",
                "目前播放時間必須晚於所選字幕的開始時間，才能設為結束時間。"
            )
            return
        self.snapshot()
        for idx in selected:
            entry = entries[idx]
            entry.end_ms = current
            if side == "A":
                m = self.matches.get(idx)
                if m and not m.b_indices:
                    m.assigned_start_ms = entry.start_ms
                    m.assigned_end_ms = entry.end_ms
                    m.status = "manual_a"
        self.refresh_trees()
        self.selection_sync_suspended = True
        tree.selection_set([f"{prefix}{i}" for i in selected if tree.exists(f"{prefix}{i}")])
        if selected and tree.exists(f"{prefix}{selected[0]}"):
            tree.see(f"{prefix}{selected[0]}")
        self.after(250, self._resume_selection_sync)
        self.schedule_autosave()
        self.set_status(f"已將目前播放時間設為所選 {side} 字幕結束。")

    def add_subtitle_at_media(self, side: str):
        if not self.media_path:
            messagebox.showwarning("尚未載入媒體", "請先載入影片或音檔。")
            return
        current = self.current_media_time()
        temp = SrtEntry(0, current, current + 2000, "")
        setattr(temp, "edit_state", "added")
        entry = self.subtitle_dialog(f"目前時間新增字幕到 {side}", temp)
        if not entry:
            return
        self.snapshot()
        entries = self.a_entries if side == "A" else self.b_entries
        pos = next((i for i, x in enumerate(entries) if entry.start_ms < x.start_ms), len(entries))
        entries.insert(pos, entry)

        if side == "A":
            shifted = {}
            for ai, m in self.matches.items():
                nai = ai + 1 if ai >= pos else ai
                shifted[nai] = MatchInfo(
                    nai, list(m.b_indices), m.status, m.confidence, m.note,
                    m.assigned_start_ms, m.assigned_end_ms
                )
            self.matches = shifted
            self.renumber_entries()
            self.refresh_trees()
            self.a_tree.selection_set(f"a{pos}")
            self.a_tree.see(f"a{pos}")
        else:
            shifted_matches = {}
            for ai, m in self.matches.items():
                shifted_b = [bi + 1 if bi >= pos else bi for bi in m.b_indices]
                shifted_matches[ai] = MatchInfo(
                    ai, shifted_b, m.status, m.confidence, m.note,
                    m.assigned_start_ms, m.assigned_end_ms
                )
            self.matches = shifted_matches
            self.b_unique = {bi + 1 if bi >= pos else bi for bi in self.b_unique}
            self.renumber_entries()
            self.refresh_trees()
            self.b_tree.selection_set(f"b{pos}")
            self.b_tree.see(f"b{pos}")

        self.schedule_autosave()
        self.set_status(f"已在目前播放時間新增 {side} 字幕。")

    def load_a(self):
        path = filedialog.askopenfilename(title="選擇 A 人工字幕", filetypes=[("SRT 字幕", "*.srt"), ("所有檔案", "*.*")])
        if not path:
            return
        try:
            self.a_entries = parse_srt(path)
            self.a_path = path
            self.a_label.config(text=Path(path).name)
            self.matches.clear()
            self.b_unique.clear()
            self.refresh_trees()
            self.schedule_autosave()
            self.set_status(f"A 已載入 {len(self.a_entries)} 條。")
        except Exception as e:
            messagebox.showerror("載入失敗", str(e))

    def load_b(self):
        path = filedialog.askopenfilename(title="選擇 B AI 字幕", filetypes=[("SRT 字幕", "*.srt"), ("所有檔案", "*.*")])
        if not path:
            return
        try:
            self.b_entries = parse_srt(path)
            self.b_path = path
            self.b_label.config(text=Path(path).name)
            self.matches.clear()
            self.b_unique.clear()
            self.refresh_trees()
            self.schedule_autosave()
            self.set_status(f"B 已載入 {len(self.b_entries)} 條。")
        except Exception as e:
            messagebox.showerror("載入失敗", str(e))

    def find_match_conflicts(self):
        """
        找出容易造成重複輸出的結構性配對衝突。

        典型例：
          A1 -> B1
          A2 -> B1, B2

        同一個 B 被多條 A 使用，其中至少一條 A 還同時使用其他 B。
        這通常是配對範圍互相包覆，輸出後很可能重複或重疊。
        """
        reverse: Dict[int, List[int]] = {}
        for ai, match in self.matches.items():
            if not (0 <= ai < len(self.a_entries)):
                continue
            for bi in match.b_indices:
                if 0 <= bi < len(self.b_entries):
                    reverse.setdefault(bi, []).append(ai)

        conflicts = []
        seen = set()
        for bi, ais in reverse.items():
            unique_ais = sorted(set(ais))
            if len(unique_ais) < 2:
                continue

            # 只要共用此 B 的 A 中，有任一條還配到其他 B，就視為包覆型衝突。
            expanded = [ai for ai in unique_ais if len(self.matches[ai].b_indices) > 1]
            if not expanded:
                continue

            key = (bi, tuple(unique_ais))
            if key in seen:
                continue
            seen.add(key)
            conflicts.append({
                "b_index": bi,
                "a_indices": unique_ais,
                "expanded_a_indices": expanded,
            })

        conflicts.sort(
            key=lambda x: (
                self.b_entries[x["b_index"]].start_ms,
                x["b_index"],
            )
        )
        return conflicts

    def _refresh_match_conflict_cache(self):
        conflicts = self.find_match_conflicts()
        self.match_conflict_indices = [item["b_index"] for item in conflicts]
        if self.match_conflict_cursor >= len(conflicts):
            self.match_conflict_cursor = -1
        return conflicts

    def select_match_conflict(self, conflict):
        bi = conflict["b_index"]
        ais = conflict["a_indices"]
        self.selection_sync_suspended = True
        try:
            self.a_tree.selection_set(
                [f"a{ai}" for ai in ais if self.a_tree.exists(f"a{ai}")]
            )
            self.b_tree.selection_set(
                f"b{bi}" if self.b_tree.exists(f"b{bi}") else ()
            )
            if ais and self.a_tree.exists(f"a{ais[0]}"):
                self.a_tree.focus(f"a{ais[0]}")
                self.a_tree.see(f"a{ais[0]}")
            if self.b_tree.exists(f"b{bi}"):
                self.b_tree.focus(f"b{bi}")
                self.b_tree.see(f"b{bi}")
        finally:
            self.after(350, self._resume_selection_sync)

        a_numbers = ", ".join(str(self.a_entries[ai].number) for ai in ais)
        b_number = self.b_entries[bi].number
        conflicts = self.find_match_conflicts()
        try:
            position = next(
                i + 1 for i, item in enumerate(conflicts)
                if item["b_index"] == bi and item["a_indices"] == ais
            )
        except StopIteration:
            position = 1
        self.update_review_counters()
        self.set_status(
            f"配對衝突：第 {position}/{len(conflicts)} 組｜"
            f"A {a_numbers} 共用 B {b_number}，且其中一條 A 還配到其他 B。"
            "可調整選取後按 Enter 重新配對。"
        )

    def jump_match_conflict(self, direction: int):
        conflicts = self._refresh_match_conflict_cache()
        if not conflicts:
            messagebox.showinfo("配對衝突", "目前沒有偵測到包覆型配對衝突。")
            return

        if direction > 0:
            self.match_conflict_cursor = (self.match_conflict_cursor + 1) % len(conflicts)
        else:
            if self.match_conflict_cursor < 0:
                self.match_conflict_cursor = 0
            self.match_conflict_cursor = (self.match_conflict_cursor - 1) % len(conflicts)

        self.select_match_conflict(conflicts[self.match_conflict_cursor])

    def show_match_conflict_report(self):
        conflicts = self._refresh_match_conflict_cache()
        if not conflicts:
            messagebox.showinfo(
                "配對衝突檢查",
                "目前沒有偵測到這類配對：\n\n"
                "A1 → B1\n"
                "A2 → B1、B2"
            )
            return

        dlg = tk.Toplevel(self)
        dlg.title(f"配對衝突：{len(conflicts)} 組")
        dlg.transient(self)
        dlg.geometry("760x440")

        ttk.Label(
            dlg,
            text=(
                "下列配對很可能造成重複／重疊輸出。\n"
                "雙擊任一列即可跳回 A、B 清單並同時選取相關字幕，再按 Enter 重新配對。"
            ),
            padding=(10, 10, 10, 6),
        ).pack(fill="x")

        tree = ttk.Treeview(
            dlg,
            columns=("a", "b", "detail"),
            show="headings",
            selectmode="browse",
        )
        tree.heading("a", text="相關 A")
        tree.heading("b", text="共用 B")
        tree.heading("detail", text="衝突原因")
        tree.column("a", width=190, anchor="w")
        tree.column("b", width=90, anchor="center")
        tree.column("detail", width=430, anchor="w")
        tree.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        for i, item in enumerate(conflicts):
            ais = item["a_indices"]
            expanded = item["expanded_a_indices"]
            a_numbers = ", ".join(str(self.a_entries[ai].number) for ai in ais)
            expanded_numbers = ", ".join(str(self.a_entries[ai].number) for ai in expanded)
            bi = item["b_index"]
            tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    a_numbers,
                    self.b_entries[bi].number,
                    f"A {expanded_numbers} 同時還配到其他 B",
                ),
            )

        button_bar = ttk.Frame(dlg, padding=(10, 0, 10, 10))
        button_bar.pack(fill="x")

        def go_selected(_event=None):
            selected = tree.selection()
            if not selected:
                return
            idx = int(selected[0])
            self.match_conflict_cursor = idx
            self.select_match_conflict(conflicts[idx])
            dlg.destroy()

        ttk.Button(button_bar, text="跳到所選衝突", command=go_selected).pack(side="left")
        ttk.Button(button_bar, text="關閉", command=dlg.destroy).pack(side="right")
        tree.bind("<Double-1>", go_selected)
        if conflicts:
            tree.selection_set("0")
            tree.focus("0")

    def refresh_trees(self):
        self.normalize_legacy_project_state()
        self.syncing_selection = True
        self.a_tree.delete(*self.a_tree.get_children())
        self.b_tree.delete(*self.b_tree.get_children())
        reverse: Dict[int, List[int]] = {}
        for ai, m in self.matches.items():
            for bi in m.b_indices:
                reverse.setdefault(bi, []).append(ai)

        conflicts = self.find_match_conflicts()
        conflict_a = {
            ai for item in conflicts for ai in item["a_indices"]
        }
        conflict_b = {
            item["b_index"] for item in conflicts
        }

        for i, e in enumerate(self.a_entries):
            m = self.matches.get(i)
            if m:
                if m.b_indices:
                    desc = "B " + ",".join(str(self.b_entries[j].number) for j in m.b_indices if j < len(self.b_entries))
                    tag = "conflict" if i in conflict_a else ("ai" if m.status == "ai" else "matched")
                else:
                    desc = "無對應"
                    tag = "unmatched"
            else:
                desc, tag = "", ""
            self.a_tree.insert("", "end", iid=f"a{i}", values=(e.number, e.start, e.end, e.text.replace("\n", " / "), desc), tags=(tag,) if tag else ())

        for i, e in enumerate(self.b_entries):
            if i in reverse:
                desc = "A " + ",".join(str(self.a_entries[j].number) for j in reverse[i] if j < len(self.a_entries))
                is_ai = any(self.matches[j].status == "ai" for j in reverse[i])
                tag = "conflict" if i in conflict_b else ("ai" if is_ai else "matched")
            else:
                desc, tag = "", ""
            self.b_tree.insert("", "end", iid=f"b{i}", values=(e.number, e.start, e.end, e.text.replace("\n", " / "), desc), tags=(tag,) if tag else ())
        self.after_idle(self._finish_selection_sync)
        self.update_review_counters()
        self.schedule_preview_subtitle_refresh()

    def on_tree_mouse_press(self, tree: ttk.Treeview, side: str, event):
        """Ctrl／Shift 選取時暫停 A↔B 自動同步，讓兩側可以同時保留選取。"""
        ctrl_pressed = bool(event.state & 0x0004)
        shift_pressed = bool(event.state & 0x0001)

        if ctrl_pressed or shift_pressed:
            self.selection_sync_suspended = True
            if self.selection_sync_after_id:
                try:
                    self.after_cancel(self.selection_sync_after_id)
                except Exception:
                    pass
            self.selection_sync_after_id = self.after(250, self._resume_selection_sync)

        self.clear_on_blank_click(tree, event)

    def _resume_selection_sync(self):
        self.selection_sync_after_id = None
        self.selection_sync_suspended = False
        self.update_status()

    def clear_on_blank_click(self, tree: ttk.Treeview, event):
        # 點到列表空白區時，只清除該側選取。
        if not tree.identify_row(event.y):
            self.clear_tree_selection(tree)

    def renumber_entries(self):
        for i, e in enumerate(self.a_entries, 1):
            e.number = i
        for i, e in enumerate(self.b_entries, 1):
            e.number = i

    def schedule_autosave(self):
        if self.autosave_after_id is not None:
            try:
                self.after_cancel(self.autosave_after_id)
            except Exception:
                pass
        self.autosave_after_id = self.after(400, self.autosave_now)

    def autosave_now(self):
        self.autosave_after_id = None
        if not self.a_entries and not self.b_entries:
            return
        data = {
            "version": APP_VERSION,
            "a_path": self.a_path,
            "b_path": self.b_path,
            "media_path": self.media_path,
            "a_entries": [asdict(e) for e in self.a_entries],
            "b_entries": [asdict(e) for e in self.b_entries],
            "matches": {str(k): asdict(v) for k, v in self.matches.items()},
            "b_unique": sorted(self.b_unique),
            "keep_unmatched_a": self.keep_a_var.get(),
            "keep_unmatched_a": self.keep_a_var.get(),
            "keep_unmatched_b": self.keep_b_var.get(),
            "active_profile": self.active_profile_var.get(),
        }
        try:
            AUTOSAVE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def offer_autosave_recovery(self):
        if not AUTOSAVE_FILE.exists():
            return
        try:
            data = json.loads(AUTOSAVE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        if not data.get("a_entries") and not data.get("b_entries"):
            return
        if messagebox.askyesno("發現自動暫存", "發現上次未完成內容，是否恢復？"):
            self.restore_from_data(data)
            self.set_status("已恢復自動暫存內容。")

    def restore_from_data(self, data):
        self.a_entries = [SrtEntry(**x) for x in data.get("a_entries", [])]
        self.b_entries = [SrtEntry(**x) for x in data.get("b_entries", [])]
        self.a_path = data.get("a_path", "")
        self.b_path = data.get("b_path", "")
        self.media_path = data.get("media_path", "")
        if self.media_path:
            self.media_name_var.set(Path(self.media_path).name + "（尚未重新載入）")
        self.a_label.config(text=Path(self.a_path).name if self.a_path else "暫存內容")
        self.b_label.config(text=Path(self.b_path).name if self.b_path else "暫存內容")
        self.matches = {}
        for k, v in data.get("matches", {}).items():
            self.matches[int(k)] = MatchInfo(
                int(v.get("a_index", k)), list(v.get("b_indices", [])),
                v.get("status", "manual"), float(v.get("confidence", 1)),
                v.get("note", ""), v.get("assigned_start_ms"), v.get("assigned_end_ms")
            )
        self.b_unique = set(data.get("b_unique", []))
        self.keep_a_var.set(bool(data.get("keep_unmatched_a", True)))
        self.keep_b_var.set(bool(data.get("keep_unmatched_b", True)))
        if data.get("active_profile"):
            self.active_profile_var.set(data["active_profile"])
        self.renumber_entries()
        self.refresh_trees()

    def selected_indices(self, tree: ttk.Treeview, prefix: str) -> List[int]:
        result = []
        for iid in tree.selection():
            try:
                if iid.startswith(prefix):
                    index = int(iid[len(prefix):])
                    limit = len(self.a_entries) if prefix == "a" else len(self.b_entries)
                    if 0 <= index < limit:
                        result.append(index)
            except (TypeError, ValueError, tk.TclError):
                continue
        return sorted(set(result))

    def snapshot(self):
        a_copy = [SrtEntry(e.number, e.start_ms, e.end_ms, e.text, e.uid) for e in self.a_entries]
        b_copy = [SrtEntry(e.number, e.start_ms, e.end_ms, e.text, e.uid) for e in self.b_entries]
        copied = {
            k: MatchInfo(v.a_index, list(v.b_indices), v.status, v.confidence, v.note,
                         v.assigned_start_ms, v.assigned_end_ms)
            for k, v in self.matches.items()
        }
        self.undo_stack.append((a_copy, b_copy, copied, set(self.b_unique)))
        if len(self.undo_stack) > 100:
            self.undo_stack.pop(0)

    def set_unmatched_or_unlink(self):
        """將目前所選 A 或 B 的配對關係解除，不另外建立無對應狀態。"""
        a_sel = self.selected_indices(self.a_tree, "a")
        b_sel = self.selected_indices(self.b_tree, "b")
        if not a_sel and not b_sel:
            messagebox.showwarning("尚未選取", "請先選取 A 或 B 字幕。")
            return

        self.snapshot()
        changed = False

        # 選取 A：移除該 A 的全部 B 配對。
        for ai in a_sel:
            if ai in self.matches:
                del self.matches[ai]
                changed = True

        # 選取 B：從所有 A 配對中移除這些 B。
        selected_b = set(b_sel)
        if selected_b:
            for ai in list(self.matches):
                match = self.matches[ai]
                remaining = [bi for bi in match.b_indices if bi not in selected_b]
                if len(remaining) != len(match.b_indices):
                    changed = True
                    if remaining:
                        match.b_indices = remaining
                        match.status = "manual"
                        match.note = "部分解除配對"
                    else:
                        del self.matches[ai]

        # 舊版專案可能保存 B 獨有標記；統一清除，不再使用此狀態。
        if hasattr(self, "b_unique"):
            before = len(self.b_unique)
            self.b_unique.difference_update(selected_b)
            changed = changed or len(self.b_unique) != before

        if not changed:
            self.set_status("所選字幕原本就沒有配對。")
            return

        self.refresh_trees()
        self.schedule_autosave()
        if hasattr(self, "schedule_preview_subtitle_refresh"):
            try:
                self.schedule_preview_subtitle_refresh()
            except Exception:
                pass
        self.set_status("已解除所選字幕配對。")

    def apply_match(self, move_next: bool = False):
        a_sel = self.selected_indices(self.a_tree, "a")
        b_sel = self.selected_indices(self.b_tree, "b")
        if not a_sel or not b_sel:
            messagebox.showwarning("尚未選取", "請在左側選 A 字幕，右側選 B 字幕。")
            return
        self.snapshot()
        if len(a_sel) == 1:
            ai = a_sel[0]
            self.matches[ai] = MatchInfo(ai, b_sel, "manual", 1.0)
        elif len(b_sel) == 1:
            # 多個 A 對一個 B：預設合併成一條多行 A，再套用該 B 的完整時間。
            # 只有使用者明確按「平分」按鈕時才切割時間。
            bi = b_sel[0]
            merged_ai = self._merge_a_for_match(a_sel)
            self.matches[merged_ai] = MatchInfo(
                merged_ai, [bi], "manual_merged", 1.0, "多條 A 合併後對應一條 B"
            )
            a_sel = [merged_ai]
        elif len(a_sel) == len(b_sel):
            for ai, bi in zip(a_sel, b_sel):
                self.matches[ai] = MatchInfo(ai, [bi], "manual", 1.0)
        else:
            if not messagebox.askyesno("多對多配對", "A、B 選取數量不同。要把每一條 A 都套用整段 B 時間範圍嗎？"):
                return
            for ai in a_sel:
                self.matches[ai] = MatchInfo(ai, b_sel, "manual", 1.0)

        for bi in b_sel:
            self.b_unique.discard(bi)
        a_numbers = ",".join(str(self.a_entries[i].number) for i in a_sel if 0 <= i < len(self.a_entries))
        b_numbers = ",".join(str(self.b_entries[i].number) for i in b_sel if 0 <= i < len(self.b_entries))
        self.refresh_trees()
        self.schedule_autosave()
        if move_next:
            self.select_next_after(a_sel[-1], b_sel[-1])
            if a_sel[-1] + 1 < len(self.a_entries) and b_sel[-1] + 1 < len(self.b_entries):
                self.set_status(f"已配對 A {a_numbers} → B {b_numbers}；已前往下一組。")
        else:
            self.set_status(f"已配對 A {a_numbers} → B {b_numbers}")

    def apply_match_and_next(self):
        self.apply_match(True)

    def select_next_after(self, ai: int, bi: int):
        """配對後讓 A、B 各自前進一條；到底的一側取消選取，避免誤重複配對。"""
        next_a = ai + 1 if ai + 1 < len(self.a_entries) else None
        next_b = bi + 1 if bi + 1 < len(self.b_entries) else None

        self.selection_sync_suspended = True
        try:
            self.a_tree.selection_remove(*self.a_tree.selection())
            self.b_tree.selection_remove(*self.b_tree.selection())

            if next_a is not None and self.a_tree.exists(f"a{next_a}"):
                self.a_tree.selection_set(f"a{next_a}")
                self.a_tree.focus(f"a{next_a}")
                self.a_tree.see(f"a{next_a}")

            if next_b is not None and self.b_tree.exists(f"b{next_b}"):
                self.b_tree.selection_set(f"b{next_b}")
                self.b_tree.focus(f"b{next_b}")
                self.b_tree.see(f"b{next_b}")
        finally:
            self.after(300, self._resume_selection_sync)

        if next_a is None or next_b is None:
            reached = []
            if next_a is None:
                reached.append("A")
            if next_b is None:
                reached.append("B")
            self.set_status(f"已配對；{'、'.join(reached)} 已到最後一條。")

    def _merge_a_for_match(self, selected: List[int]) -> int:
        """在已 snapshot 的情況下合併 A，回傳合併後索引。"""
        selected = sorted(selected)
        first = selected[0]
        merged = SrtEntry(
            0,
            min(self.a_entries[i].start_ms for i in selected),
            max(self.a_entries[i].end_ms for i in selected),
            "\n".join(self.a_entries[i].text for i in selected),
        )
        self.a_entries[first] = merged
        self._delete_entries_without_prompt(selected[1:], [])
        self.renumber_entries()
        return first

    def merge_many_a_to_one_b_in_selection(self):
        """
        將所選範圍內「多條 A 各自對應同一條 B」的群組合併成一條多行 A。
        不切割 B 時間；合併後直接採用該 B 的完整時間。
        """
        selected_a = self.selected_indices(self.a_tree, "a")
        selected_b = self.selected_indices(self.b_tree, "b")

        if not selected_a and not selected_b:
            messagebox.showwarning(
                "尚未選取範圍",
                "請先在 A 或 B 選取要整理的範圍。\n"
                "可用 Shift 選連續範圍，或 Ctrl 加選。"
            )
            return

        selected_a_objects = {id(self.a_entries[i]) for i in selected_a}
        selected_b_set = set(selected_b)

        def in_a_scope(ai: int) -> bool:
            return not selected_a_objects or id(self.a_entries[ai]) in selected_a_objects

        candidate_b = set()
        for ai, match in self.matches.items():
            if not (0 <= ai < len(self.a_entries)) or not in_a_scope(ai):
                continue
            if len(match.b_indices) == 1:
                bi = match.b_indices[0]
                if 0 <= bi < len(self.b_entries) and (not selected_b_set or bi in selected_b_set):
                    candidate_b.add(bi)

        groups = []
        for bi in sorted(candidate_b):
            ais = sorted(
                ai for ai, match in self.matches.items()
                if 0 <= ai < len(self.a_entries)
                and in_a_scope(ai)
                and match.b_indices == [bi]
            )
            if len(ais) >= 2:
                groups.append((bi, ais))

        if not groups:
            messagebox.showinfo(
                "沒有可合併群組",
                "所選範圍內沒有找到「多條 A 對同一條 B」的配對。"
            )
            return

        preview = "\n".join(
            f"B{self.b_entries[bi].number}：{len(ais)} 條 A"
            for bi, ais in groups[:12]
        )
        if len(groups) > 12:
            preview += f"\n另有 {len(groups) - 12} 組"

        if not messagebox.askyesno(
            "確認自動合併",
            f"將整理 {len(groups)} 組配對：\n\n{preview}\n\n"
            "每組 A 會依原順序合併成多行，並採用對應 B 的完整時間。"
        ):
            return

        self.snapshot()
        merged_count = 0

        # B 索引不會因 A 合併而改變；每次重新尋找目前 A 索引，避免刪除後索引位移。
        for bi, _ in groups:
            ais = sorted(
                ai for ai, match in self.matches.items()
                if 0 <= ai < len(self.a_entries)
                and in_a_scope(ai)
                and match.b_indices == [bi]
            )
            if len(ais) < 2:
                continue
            merged_ai = self._merge_a_for_match(ais)
            self.matches[merged_ai] = MatchInfo(
                merged_ai,
                [bi],
                "manual_merged",
                1.0,
                "所選範圍自動合併多A→單B",
            )
            merged_count += 1

        self.refresh_trees()
        self.schedule_autosave()
        self.schedule_preview_subtitle_refresh()
        self.set_status(f"已自動合併 {merged_count} 組多 A → 單 B 配對。")

    def split_selected_a_over_b(self, mode: str = "equal"):
        """將多條 A 字幕分配到一條 B 字幕的時間範圍內。"""
        a_sel = self.selected_indices(self.a_tree, "a")
        b_sel = self.selected_indices(self.b_tree, "b")
        if len(a_sel) < 2 or len(b_sel) != 1:
            messagebox.showwarning(
                "選取方式不符",
                "請在左側選取 2 條以上 A 字幕，右側只選取 1 條 B 字幕。"
            )
            return

        bi = b_sel[0]
        b = self.b_entries[bi]
        duration = max(1, b.end_ms - b.start_ms)

        if mode == "length":
            weights = [max(1, len(re.sub(r"\\s+", "", self.a_entries[ai].text))) for ai in a_sel]
        else:
            weights = [1 for _ in a_sel]

        total_weight = sum(weights)
        self.snapshot()
        cursor = b.start_ms

        for pos, (ai, weight) in enumerate(zip(a_sel, weights)):
            if pos == len(a_sel) - 1:
                end = b.end_ms
            else:
                part = round(duration * weight / total_weight)
                end = min(b.end_ms, cursor + max(1, part))
            self.matches[ai] = MatchInfo(
                ai,
                [bi],
                "manual_split",
                1.0,
                "由一條 B 時間切分",
                cursor,
                end,
            )
            cursor = end
            total_weight -= weight
            duration = max(0, b.end_ms - cursor)

        self.b_unique.discard(bi)
        self.refresh_trees()
        self.set_status(
            f"已將 B{b.number} 的 {b.start}～{b.end} "
            f"平均切成 {len(a_sel)} 段。"
        )

    def clear_tree_selection(self, tree: ttk.Treeview):
        tree.selection_remove(tree.selection())
        self.update_status()

    def clear_all_selections(self):
        self.clear_tree_selection(self.a_tree)
        self.clear_tree_selection(self.b_tree)
        self.set_status("已取消 A、B 選取。")

    def delete_selected_a(self):
        a_sel = self.selected_indices(self.a_tree, "a")
        if not a_sel:
            messagebox.showwarning("尚未選取", "請先在左側選取要刪除的 A 字幕。")
            return
        self._delete_entries(a_sel, [])

    def delete_selected_b(self):
        b_sel = self.selected_indices(self.b_tree, "b")
        if not b_sel:
            messagebox.showwarning("尚未選取", "請先在右側選取要刪除的 B 字幕。")
            return
        self._delete_entries([], b_sel)

    def _delete_entries(self, a_sel, b_sel):
        if not messagebox.askyesno(
            "刪除字幕",
            f"確定刪除 A {len(a_sel)} 條、B {len(b_sel)} 條？\n可用 Ctrl+Z 復原。"
        ):
            return

        self.snapshot()
        ar, br = set(a_sel), set(b_sel)
        amap, bmap, newa, newb = {}, {}, [], []

        for i, e in enumerate(self.a_entries):
            if i not in ar:
                amap[i] = len(newa)
                newa.append(e)

        for i, e in enumerate(self.b_entries):
            if i not in br:
                bmap[i] = len(newb)
                newb.append(e)

        new_matches = {}
        for ai, m in self.matches.items():
            if ai not in amap:
                continue
            nai = amap[ai]
            nbis = [bmap[x] for x in m.b_indices if x in bmap]

            # 若刪掉的是已配對 B，A 保留但改為無對應，並沿用 A 原始時間。
            if m.b_indices and not nbis:
                a_entry = newa[nai]
                new_matches[nai] = MatchInfo(
                    nai, [], "unmatched", 0.0, "原配對 B 已刪除",
                    a_entry.start_ms, a_entry.end_ms
                )
            else:
                new_matches[nai] = MatchInfo(
                    nai, nbis, m.status, m.confidence, m.note,
                    m.assigned_start_ms, m.assigned_end_ms
                )

        self.a_entries = newa
        self.b_entries = newb
        self.matches = new_matches
        self.b_unique = {bmap[x] for x in self.b_unique if x in bmap}

        self.renumber_entries()
        self.refresh_trees()
        self.clear_all_selections()
        self.schedule_autosave()
        self.set_status(f"已刪除 A {len(a_sel)} 條、B {len(b_sel)} 條字幕。")

    def subtitle_dialog(self, title, entry=None):
        dlg = tk.Toplevel(self); dlg.title(title); dlg.geometry("620x360"); dlg.transient(self); dlg.grab_set()
        start_var = tk.StringVar(value=entry.start if entry else "00:00:00,000")
        end_var = tk.StringVar(value=entry.end if entry else "00:00:02,000")
        result = {}
        f = ttk.Frame(dlg, padding=12); f.pack(fill="both", expand=True)
        ttk.Label(f, text="開始時間").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(f, textvariable=start_var).grid(row=0, column=1, sticky="ew")
        ttk.Label(f, text="結束時間").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(f, textvariable=end_var).grid(row=1, column=1, sticky="ew")
        ttk.Label(f, text="字幕文字").grid(row=2, column=0, sticky="nw", pady=5)
        box = tk.Text(f, height=10, wrap="word"); box.grid(row=2, column=1, sticky="nsew")
        if entry: box.insert("1.0", entry.text)
        f.columnconfigure(1, weight=1); f.rowconfigure(2, weight=1)
        def ok():
            try:
                s, e = srt_to_ms(start_var.get()), srt_to_ms(end_var.get())
                text = box.get("1.0", "end").strip()
                if e <= s: raise ValueError("結束時間必須晚於開始時間。")
                if not text: raise ValueError("字幕文字不可空白。")
                result["entry"] = SrtEntry(entry.number if entry else 0, s, e, text)
                dlg.destroy()
            except Exception as ex:
                messagebox.showerror("輸入錯誤", str(ex), parent=dlg)
        bf = ttk.Frame(f); bf.grid(row=3, column=0, columnspan=2, sticky="e", pady=10)
        ttk.Button(bf, text="取消", command=dlg.destroy).pack(side="right")
        ttk.Button(bf, text="確定", command=ok).pack(side="right", padx=5)
        self._protect_dialog_undo(dlg)
        self.wait_window(dlg)
        return result.get("entry")

    def _enable_local_text_undo(self, widget):
        """編輯視窗中的 Ctrl+Z/Ctrl+Y 只復原文字欄位，不觸發主程式復原。"""
        try:
            widget.configure(undo=True, autoseparators=True, maxundo=-1)
        except tk.TclError:
            return

        def local_undo(_event=None):
            try:
                widget.edit_undo()
            except tk.TclError:
                pass
            return "break"

        def local_redo(_event=None):
            try:
                widget.edit_redo()
            except tk.TclError:
                pass
            return "break"

        widget.bind("<Control-z>", local_undo)
        widget.bind("<Control-Z>", local_undo)
        widget.bind("<Control-y>", local_redo)
        widget.bind("<Control-Y>", local_redo)
        widget.bind("<Control-Shift-Z>", local_redo)

    def _enable_local_entry_undo(self, widget):
        """Entry/Spinbox 使用 Tk 內建編輯事件，並阻止 Ctrl+Z 傳到主視窗。"""
        history = [widget.get()]
        cursor = [0]
        internal = [False]

        def remember(_event=None):
            if internal[0]:
                return
            value = widget.get()
            if history[cursor[0]] == value:
                return
            del history[cursor[0] + 1:]
            history.append(value)
            cursor[0] = len(history) - 1

        def apply_history(index):
            internal[0] = True
            try:
                widget.delete(0, "end")
                widget.insert(0, history[index])
                widget.icursor("end")
            finally:
                internal[0] = False

        def local_undo(_event=None):
            remember()
            if cursor[0] > 0:
                cursor[0] -= 1
                apply_history(cursor[0])
            return "break"

        def local_redo(_event=None):
            if cursor[0] + 1 < len(history):
                cursor[0] += 1
                apply_history(cursor[0])
            return "break"

        widget.bind("<KeyRelease>", remember, add="+")
        widget.bind("<FocusOut>", remember, add="+")
        widget.bind("<Control-z>", local_undo)
        widget.bind("<Control-Z>", local_undo)
        widget.bind("<Control-y>", local_redo)
        widget.bind("<Control-Y>", local_redo)
        widget.bind("<Control-Shift-Z>", local_redo)

    def _protect_dialog_undo(self, dialog):
        """讓編輯對話框內的 Ctrl+Z 優先由目前輸入元件處理。"""
        for child in dialog.winfo_children():
            self._protect_dialog_widget(child)

    def _protect_dialog_widget(self, widget):
        if isinstance(widget, tk.Text):
            self._enable_local_text_undo(widget)
        elif isinstance(widget, (ttk.Entry, tk.Entry, ttk.Spinbox, tk.Spinbox)):
            self._enable_local_entry_undo(widget)
        for child in widget.winfo_children():
            self._protect_dialog_widget(child)

    def add_a_subtitle(self):
        e = self.subtitle_dialog("新增 A 字幕")
        if not e: return
        self.snapshot()
        pos = next((i for i, x in enumerate(self.a_entries) if e.start_ms < x.start_ms), len(self.a_entries))
        self.a_entries.insert(pos, e)
        shifted = {}
        for ai, m in self.matches.items():
            nai = ai + 1 if ai >= pos else ai
            shifted[nai] = MatchInfo(nai, list(m.b_indices), m.status, m.confidence, m.note,
                                     m.assigned_start_ms, m.assigned_end_ms)
        self.matches = shifted
        self.renumber_entries()
        self.refresh_trees()
        self.a_tree.selection_set(f"a{pos}")
        self.a_tree.see(f"a{pos}")
        self.schedule_autosave()

    def edit_selected_a(self):
        sel = self.selected_indices(self.a_tree, "a")
        if len(sel) != 1:
            messagebox.showwarning("請選一條", "請只選取一條 A 字幕。")
            return
        ai = sel[0]
        e = self.subtitle_dialog("編輯所選 A", self.a_entries[ai])
        if not e: return
        self.snapshot()
        e.number = self.a_entries[ai].number
        self.a_entries[ai] = e
        m = self.matches.get(ai)
        if not m:
            self.matches[ai] = MatchInfo(ai, [], "manual_a", 1.0, "A 手動時間", e.start_ms, e.end_ms)
        elif not m.b_indices:
            m.assigned_start_ms, m.assigned_end_ms, m.status = e.start_ms, e.end_ms, "manual_a"
        self.refresh_trees()
        self.schedule_autosave()

    def edit_selected_b(self):
        sel = self.selected_indices(self.b_tree, "b")
        if len(sel) != 1:
            messagebox.showwarning("請選一條", "請只選取一條 B 字幕。")
            return
        bi = sel[0]
        e = self.subtitle_dialog("編輯所選 B", self.b_entries[bi])
        if not e:
            return
        self.snapshot()
        e.number = self.b_entries[bi].number
        self.b_entries[bi] = e
        self.refresh_trees()
        self.b_tree.selection_set(f"b{bi}")
        self.b_tree.see(f"b{bi}")
        self.schedule_autosave()
        self.set_status("已編輯所選 B 字幕。")

    def merge_selected_subtitles(self, side: str):
        tree = self.a_tree if side == "A" else self.b_tree
        prefix = "a" if side == "A" else "b"
        entries = self.a_entries if side == "A" else self.b_entries
        selected = self.selected_indices(tree, prefix)
        if len(selected) < 2:
            messagebox.showwarning("選取不足", f"請選取至少兩條 {side} 字幕。")
            return
        selected = sorted(selected)
        if selected != list(range(selected[0], selected[-1] + 1)):
            if not messagebox.askyesno("非連續選取", "選取的字幕不連續，仍要依時間順序合併嗎？"):
                return
        self.snapshot()
        merged = SrtEntry(0, min(entries[i].start_ms for i in selected), max(entries[i].end_ms for i in selected), "\n".join(entries[i].text for i in selected))
        first = selected[0]
        remove = set(selected[1:])
        entries[first] = merged
        if side == "A":
            self._delete_entries_without_prompt(sorted(remove), [])
        else:
            self._delete_entries_without_prompt([], sorted(remove))
        self.renumber_entries()
        self.refresh_trees()
        tree.selection_set(f"{prefix}{first}")
        tree.see(f"{prefix}{first}")
        self.schedule_autosave()
        self.set_status(f"已將 {len(selected)} 條 {side} 字幕合併為一條。")

    def _delete_entries_without_prompt(self, a_sel, b_sel):
        ar, br = set(a_sel), set(b_sel)
        amap, bmap, newa, newb = {}, {}, [], []
        for i, e in enumerate(self.a_entries):
            if i not in ar:
                amap[i] = len(newa); newa.append(e)
        for i, e in enumerate(self.b_entries):
            if i not in br:
                bmap[i] = len(newb); newb.append(e)
        new_matches = {}
        for ai, m in self.matches.items():
            if ai not in amap:
                continue
            nai = amap[ai]
            nbis = [bmap[x] for x in m.b_indices if x in bmap]
            new_matches[nai] = MatchInfo(nai, nbis, m.status, m.confidence, m.note, m.assigned_start_ms, m.assigned_end_ms)
        self.a_entries, self.b_entries, self.matches = newa, newb, new_matches
        self.b_unique = {bmap[x] for x in self.b_unique if x in bmap}

    def open_find_dialog(self, event=None):
        if self.find_dialog is not None and self.find_dialog.winfo_exists():
            self.find_dialog.deiconify()
            self.find_dialog.lift()
            self.find_dialog.focus_force()
            return "break"

        dlg = tk.Toplevel(self)
        dlg.title("搜尋字幕")
        dlg.transient(self)
        dlg.resizable(False, False)
        self.find_dialog = dlg

        frame = ttk.Frame(dlg, padding=12)
        frame.pack(fill="both", expand=True)

        self.find_query_var = tk.StringVar(value=self.find_text)
        self.find_side_var = tk.StringVar(value=self.find_side if self.find_side in ("A", "B", "A+B") else "A+B")

        ttk.Label(frame, text="搜尋文字").grid(row=0, column=0, sticky="w", pady=4)
        query_entry = ttk.Entry(frame, textvariable=self.find_query_var, width=44)
        query_entry.grid(row=0, column=1, columnspan=3, sticky="ew", pady=4)

        ttk.Label(frame, text="搜尋範圍").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(
            frame,
            textvariable=self.find_side_var,
            values=("A", "B", "A+B"),
            state="readonly",
            width=12,
        ).grid(row=1, column=1, sticky="w", pady=4)

        ttk.Button(frame, text="上一個  Shift+F3", command=self.find_previous).grid(row=2, column=1, sticky="ew", padx=(0, 4), pady=(8, 0))
        ttk.Button(frame, text="下一個  F3", command=self.find_next).grid(row=2, column=2, sticky="ew", padx=4, pady=(8, 0))
        ttk.Button(frame, text="關閉", command=self.close_find_dialog).grid(row=2, column=3, sticky="ew", padx=(4, 0), pady=(8, 0))

        def submit_next(_event=None):
            self.find_next()
            return "break"

        query_entry.bind("<Return>", submit_next)
        dlg.protocol("WM_DELETE_WINDOW", self.close_find_dialog)
        query_entry.focus_set()
        query_entry.selection_range(0, "end")
        return "break"

    def close_find_dialog(self):
        if self.find_dialog is not None:
            try:
                self.find_dialog.destroy()
            except Exception:
                pass
        self.find_dialog = None

    def _find_side_order(self):
        side = self.find_side_var.get() if self.find_side_var is not None else self.find_side
        self.find_side = side
        if side == "A":
            return [("A", self.a_tree, self.a_entries, "a")]
        if side == "B":
            return [("B", self.b_tree, self.b_entries, "b")]
        return [
            ("A", self.a_tree, self.a_entries, "a"),
            ("B", self.b_tree, self.b_entries, "b"),
        ]

    def _find_result(self, direction: int):
        query = self.find_query_var.get() if self.find_query_var is not None else self.find_text
        query = (query or "").strip()
        if not query:
            messagebox.showwarning("尚未輸入", "請輸入要搜尋的字幕文字。", parent=self.find_dialog or self)
            return

        self.find_text = query
        q = query.casefold()
        sides = self._find_side_order()
        if direction < 0:
            sides = list(reversed(sides))

        for side, tree, entries, prefix in sides:
            selected = self.selected_indices(tree, prefix)
            if direction > 0:
                anchor = selected[-1] if selected else -1
                order = list(range(anchor + 1, len(entries))) + list(range(0, anchor + 1))
            else:
                anchor = selected[0] if selected else len(entries)
                order = list(range(anchor - 1, -1, -1)) + list(range(len(entries) - 1, anchor - 1, -1))

            for i in order:
                if q in entries[i].text.casefold():
                    self.syncing_selection = True
                    try:
                        self.clear_all_selections()
                        tree.selection_set(f"{prefix}{i}")
                        tree.focus(f"{prefix}{i}")
                        tree.see(f"{prefix}{i}")
                    finally:
                        self.after_idle(self._finish_selection_sync)
                    self.set_status(
                        f"找到 {side}{entries[i].number}："
                        f"{entries[i].text.replace(chr(10), ' / ')}"
                    )
                    return

        messagebox.showinfo("搜尋完成", f"在指定範圍找不到：{query}", parent=self.find_dialog or self)

    def find_next(self, query=None):
        if query is not None:
            self.find_text = query
            if self.find_query_var is not None:
                self.find_query_var.set(query)
        self._find_result(1)

    def find_previous(self):
        self._find_result(-1)

    def open_replace_dialog(self, event=None):
        dlg = tk.Toplevel(self); dlg.title("搜尋並取代"); dlg.transient(self); dlg.grab_set(); dlg.resizable(False, False)
        f = ttk.Frame(dlg, padding=12); f.pack(fill="both", expand=True)
        find_var = tk.StringVar(value=self.find_text); repl_var = tk.StringVar(); side_var = tk.StringVar(value="A+B")
        ttk.Label(f, text="搜尋").grid(row=0, column=0, sticky="w", pady=4); ttk.Entry(f, textvariable=find_var, width=42).grid(row=0, column=1, sticky="ew")
        ttk.Label(f, text="取代為").grid(row=1, column=0, sticky="w", pady=4); ttk.Entry(f, textvariable=repl_var, width=42).grid(row=1, column=1, sticky="ew")
        ttk.Label(f, text="範圍").grid(row=2, column=0, sticky="w", pady=4); ttk.Combobox(f, textvariable=side_var, values=("A+B", "只限 A", "只限 B"), state="readonly").grid(row=2, column=1, sticky="ew")
        def replace_all():
            old, new = find_var.get(), repl_var.get()
            if not old: return
            self.snapshot(); count = 0
            targets = []
            if side_var.get() in ("A+B", "只限 A"): targets.append(self.a_entries)
            if side_var.get() in ("A+B", "只限 B"): targets.append(self.b_entries)
            for entries in targets:
                for e in entries:
                    n = e.text.count(old)
                    if n:
                        e.text = e.text.replace(old, new); count += n
            self.find_text = old; self.refresh_trees(); self.schedule_autosave(); dlg.destroy()
            self.set_status(f"已取代 {count} 處。")
        bf=ttk.Frame(f); bf.grid(row=3,column=0,columnspan=2,sticky="e",pady=(10,0)); ttk.Button(bf,text="取消",command=dlg.destroy).pack(side="right"); ttk.Button(bf,text="全部取代",command=replace_all).pack(side="right",padx=5)
        return "break"

    def schedule_preview_subtitle_refresh(self):
        if self.preview_refresh_after_id:
            try: self.after_cancel(self.preview_refresh_after_id)
            except Exception: pass
        self.preview_refresh_after_id = self.after(350, self.refresh_preview_subtitle)

    def refresh_preview_subtitle(self):
        self.preview_refresh_after_id = None
        if not self.media_path or self.vlc_player is None:
            return
        try:
            output = self.build_output()
            if not output:
                return
            preview_dir = TEMP_DIR / "preview"
            preview_dir.mkdir(parents=True, exist_ok=True)
            path = preview_dir / "final_preview.srt"
            write_srt(path, output)
            self.preview_subtitle_path = str(path)
            uri = path.resolve().as_uri()
            self.vlc_player.add_slave(self.vlc_module.MediaSlaveType.subtitle, uri, True)
            # AI 分段配對期間會逐段匯入並刷新字幕預覽，
            # 不要讓這句狀態反覆覆蓋目前的 AI 進度。
            if not self.busy:
                self.set_status("播放器字幕已更新為目前最終輸出版本。")
        except Exception:
            pass

    def mark_a_unmatched(self):
        a_sel = self.selected_indices(self.a_tree, "a")
        if not a_sel:
            return
        self.snapshot()
        for ai in a_sel:
            a = self.a_entries[ai]
            self.matches[ai] = MatchInfo(ai, [], "unmatched", 0.0, "A 無對應", a.start_ms, a.end_ms)
        self.refresh_trees()

    def mark_b_unique(self):
        b_sel = self.selected_indices(self.b_tree, "b")
        if not b_sel:
            return
        self.snapshot()
        self.b_unique.update(b_sel)
        for ai, m in list(self.matches.items()):
            if any(bi in b_sel for bi in m.b_indices):
                del self.matches[ai]
        self.refresh_trees()

    def clear_selected_match(self):
        a_sel = self.selected_indices(self.a_tree, "a")
        b_sel = set(self.selected_indices(self.b_tree, "b"))
        if not a_sel and not b_sel:
            return
        self.snapshot()
        for ai in a_sel:
            self.matches.pop(ai, None)
        if b_sel:
            self.b_unique.difference_update(b_sel)
            for ai, m in list(self.matches.items()):
                if any(b in b_sel for b in m.b_indices):
                    self.matches.pop(ai, None)
        self.refresh_trees()

    def undo(self):
        if not self.undo_stack:
            self.set_status("沒有可復原的操作。")
            return
        self.a_entries, self.b_entries, self.matches, self.b_unique = self.undo_stack.pop()
        self.renumber_entries()
        self.refresh_trees()
        self.schedule_autosave()
        self.set_status("已復原。")

    def _set_tree_selection_if_changed(self, tree: ttk.Treeview, item_ids: List[str]):
        """只在目標選取真的不同時才變更，避免 A/B TreeviewSelect 無限互相觸發。"""
        valid_ids = [iid for iid in item_ids if tree.exists(iid)]
        current = tuple(tree.selection())
        target = tuple(valid_ids)
        if current == target:
            return False

        self.syncing_selection = True
        try:
            if valid_ids:
                tree.selection_set(valid_ids)
                tree.focus(valid_ids[0])
                tree.see(valid_ids[0])
            else:
                tree.selection_remove(*tree.selection())
        finally:
            # TreeviewSelect 是排入事件佇列後才執行，不能立刻解除旗標。
            # 等 Tk 處理完本輪選取事件後再解除。
            self.after_idle(self._finish_selection_sync)
        return True

    def _finish_selection_sync(self):
        self.syncing_selection = False
        self.update_status()

    def sync_b_from_a(self, _event=None):
        if self.syncing_selection or self.selection_sync_suspended:
            self.update_status()
            return

        a_sel = self.selected_indices(self.a_tree, "a")
        if len(a_sel) == 1:
            match = self.matches.get(a_sel[0])
            b_ids = [
                f"b{bi}" for bi in (match.b_indices if match else [])
                if 0 <= bi < len(self.b_entries)
            ]
            changed = self._set_tree_selection_if_changed(self.b_tree, b_ids)
            if changed:
                return

        # 多選 A 時不強制改變 B，避免破壞使用者正在建立的多對多選取。
        self.update_status()

    def sync_a_from_b(self, _event=None):
        if self.syncing_selection or self.selection_sync_suspended:
            self.update_status()
            return

        b_sel = self.selected_indices(self.b_tree, "b")
        if len(b_sel) == 1:
            selected_b = b_sel[0]
            a_ids = [
                f"a{ai}" for ai, match in self.matches.items()
                if selected_b in match.b_indices and 0 <= ai < len(self.a_entries)
            ]
            changed = self._set_tree_selection_if_changed(self.a_tree, a_ids)
            if changed:
                return

        # 多選 B 時不強制改變 A，避免兩側選取彼此反覆覆寫。
        self.update_status()

    def update_review_counters(self):
        if not hasattr(self, "unmatched_a_nav_var"):
            return
        a_count = len(self.unmatched_indices("A"))
        b_count = len(self.unmatched_indices("B"))
        conflict_count = len(self.find_match_conflicts())
        self.unmatched_a_nav_var.set(f"無對應 A：{a_count}")
        self.unmatched_b_nav_var.set(f"無對應 B：{b_count}")
        self.conflict_nav_var.set(f"配對衝突：{conflict_count} 組")
        self.review_summary_var.set(
            f"待處理：無對應 A {a_count}｜無對應 B {b_count}｜配對衝突 {conflict_count} 組"
        )

    def unmatched_indices(self, side: str) -> List[int]:
        if side == "A":
            return [
                ai for ai in range(len(self.a_entries))
                if ai not in self.matches or not self.matches[ai].b_indices
            ]
        used_b = {
            bi for m in self.matches.values() for bi in m.b_indices
            if 0 <= bi < len(self.b_entries)
        }
        return [bi for bi in range(len(self.b_entries)) if bi not in used_b]

    def jump_unmatched(self, side: str, direction: int):
        tree = self.a_tree if side == "A" else self.b_tree
        prefix = "a" if side == "A" else "b"
        entries = self.a_entries if side == "A" else self.b_entries
        indices = self.unmatched_indices(side)
        self.update_review_counters()
        if not indices:
            messagebox.showinfo("檢查完成", f"{side} 目前沒有無對應字幕。")
            return

        current = self.selected_indices(tree, prefix)
        anchor = current[0] if current else (-1 if direction > 0 else len(entries))
        if direction > 0:
            target = next((i for i in indices if i > anchor), indices[0])
        else:
            target = next((i for i in reversed(indices) if i < anchor), indices[-1])

        position = indices.index(target) + 1
        self.selection_sync_suspended = True
        tree.selection_set(f"{prefix}{target}")
        tree.focus(f"{prefix}{target}")
        tree.see(f"{prefix}{target}")
        self.after(250, self._resume_selection_sync)
        self.set_status(
            f"無對應 {side}：第 {position}/{len(indices)} 筆｜"
            f"{side} 字幕編號 {entries[target].number}"
        )

    def update_status(self, _event=None):
        a_sel = self.selected_indices(self.a_tree, "a")
        b_sel = self.selected_indices(self.b_tree, "b")
        lines = []
        if a_sel:
            lines.append("A：\n" + "\n---\n".join(self.a_entries[i].text for i in a_sel))
        if b_sel:
            lines.append("B：\n" + "\n---\n".join(self.b_entries[i].text for i in b_sel))
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", "\n\n".join(lines))
        self.detail_text.configure(state="disabled")

    def get_active_profile(self) -> Tuple[Dict[str, Any], str]:
        name = self.active_profile_var.get()
        for p in self.config_mgr.data.get("profiles", []):
            if p.get("name") == name:
                return p, self.config_mgr.get_key(name)
        raise RuntimeError("請先建立並選擇 AI 設定檔。")

    def ai_match_selected(self):
        if self.busy:
            return
        a_sel = self.selected_indices(self.a_tree, "a")
        b_sel = self.selected_indices(self.b_tree, "b")
        if not a_sel or not b_sel:
            messagebox.showwarning("請選取範圍", "請在 A 與 B 兩側各選取一段字幕，再執行 AI 配對。")
            return

        chunk = simpledialog.askinteger(
            "所選範圍分段大小",
            "每次送給 AI 的 A 字幕數量：",
            initialvalue=80,
            minvalue=10,
            maxvalue=150,
        )
        if not chunk:
            return

        # 所選範圍也分段送出。B 依 A 在選取範圍中的相對位置切分，
        # 並在前後保留重疊字幕，避免切在拆句／合句位置。
        jobs = []
        ratio = len(b_sel) / max(1, len(a_sel))
        overlap = 12

        for offset in range(0, len(a_sel), chunk):
            a_chunk = a_sel[offset:offset + chunk]
            a_start_pos = offset
            a_end_pos = min(len(a_sel), offset + chunk)

            expected_b_start = int(a_start_pos * ratio)
            expected_b_end = int(a_end_pos * ratio)

            b_start_pos = max(0, expected_b_start - overlap)
            b_end_pos = min(len(b_sel), expected_b_end + overlap)

            b_chunk = b_sel[b_start_pos:b_end_pos]
            if a_chunk and b_chunk:
                jobs.append((a_chunk, b_chunk))

        if not jobs:
            messagebox.showwarning("無法分段", "所選範圍無法建立有效的分段工作。")
            return

        if messagebox.askyesno(
            "AI 配對所選範圍",
            f"所選 A：{len(a_sel)} 條\n"
            f"所選 B：{len(b_sel)} 條\n"
            f"將分成 {len(jobs)} 次 API 請求。\n\n"
            "每段完成後會立即顯示並自動暫存，是否開始？",
        ):
            self.start_ai_job(jobs)

    def ai_match_all(self):
        if self.busy:
            return
        if not self.a_entries or not self.b_entries:
            messagebox.showwarning("尚未載入", "請先載入 A、B 字幕。")
            return
        chunk = simpledialog.askinteger("分段大小", "每次送給 AI 的 A 字幕數量：", initialvalue=80, minvalue=10, maxvalue=150)
        if not chunk:
            return
        jobs = []
        ratio = len(self.b_entries) / max(1, len(self.a_entries))
        overlap = 15
        for a_start in range(0, len(self.a_entries), chunk):
            a_end = min(len(self.a_entries), a_start + chunk)
            center_b = int(((a_start + a_end) / 2) * ratio)
            half = int(chunk * ratio * 0.8) + overlap
            b_start = max(0, center_b - half)
            b_end = min(len(self.b_entries), center_b + half)
            jobs.append((list(range(a_start, a_end)), list(range(b_start, b_end))))
        if messagebox.askyesno("AI 分段配對全部", f"將分成 {len(jobs)} 次 API 請求。\nAI 結果仍建議人工抽查，是否開始？"):
            self.start_ai_job(jobs)

    def start_ai_job(self, jobs: List[Tuple[List[int], List[int]]]):
        try:
            profile, key = self.get_active_profile()
        except Exception as e:
            messagebox.showerror("AI 設定", str(e))
            return
        self.busy = True
        self.set_status("AI 配對中……")
        self.snapshot()
        self._new_ai_check(jobs, profile)

        def worker():
            current_chunk = 0
            current_a_ids: List[int] = []
            try:
                client = AIClient(profile, key)
                total = len(jobs)
                total_applied = 0
                for n, (a_ids, b_ids) in enumerate(jobs, 1):
                    current_chunk = n
                    current_a_ids = a_ids
                    self.after(0, lambda n=n, total=total: self.set_status(f"AI 配對中：{n}/{total}"))
                    report = self.request_match(client, a_ids, b_ids)
                    done = threading.Event()

                    def apply_chunk(report=report, n=n):
                        nonlocal total_applied
                        total_applied += self.apply_ai_results(report["results"], final=False)
                        self._update_ai_check_chunk(n, report)
                        self.schedule_autosave()
                        missing = len(report.get("unreturned_a", []))
                        suffix = f"，AI 未回傳 {missing} 筆" if missing else ""
                        self.set_status(
                            f"AI 配對中：{n}/{total}，已暫存 {total_applied} 筆{suffix}"
                        )
                        done.set()

                    self.after(0, apply_chunk)
                    done.wait()
                    if n < total:
                        time.sleep(0.3)

                self.after(
                    0,
                    lambda total_applied=total_applied, total=total:
                    self.finish_ai_job(total_applied, total)
                )
            except Exception as e:
                failed = {
                    "chunk": current_chunk,
                    "a_numbers": [
                        self.a_entries[i].number
                        for i in current_a_ids
                        if 0 <= i < len(self.a_entries)
                    ],
                    "reason": str(e),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                self.ai_check_data.setdefault("failed_chunks", []).append(failed)
                self.ai_check_data["status"] = "interrupted"
                self.ai_check_data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.save_ai_check()
                self.after(0, lambda error=str(e): self.ai_failed(error))

        threading.Thread(target=worker, daemon=True).start()

    def request_match(self, client: AIClient, a_ids: List[int], b_ids: List[int]) -> Dict[str, Any]:
        a_text = "\n".join(f"A{self.a_entries[i].number}: {self.a_entries[i].text.replace(chr(10), ' / ')}" for i in a_ids)
        b_text = "\n".join(f"B{self.b_entries[i].number}: {self.b_entries[i].text.replace(chr(10), ' / ')}" for i in b_ids)
        system = (
            "你是字幕版本配對工具。A 是人工校正版，B 是同一內容的另一影片或版本之 AI／初步字幕。"
            "請依語意與對話順序配對，即使換句話說、同義改寫、語氣不同，也可視為同一句。"
            "允許 1對1、1對多、多對1。不同版本的增刪內容可以無對應。"
            "不可只靠字面相似度。只輸出 JSON 陣列，不要解釋。"
        )
        user = f"""
請配對下列字幕。

輸出格式：
[
  {{"a": 101, "b": [98], "confidence": 0.95, "note": ""}},
  {{"a": 102, "b": [99,100], "confidence": 0.82, "note": "B 拆成兩句"}},
  {{"a": 103, "b": [], "confidence": 0.90, "note": "A 在 B 無對應"}}
]

規則：
1. a、b 使用畫面中的原字幕編號，不是陣列索引。
2. 輸入中的每一個 A 都必須輸出一次，而且只能輸出一次；即使無對應也必須輸出 b: []，不可省略。
3. 配對順序必須大致單調前進，不能前後亂跳。
4. 無把握時 confidence 降低，不要硬配。
5. 不要改寫或翻譯字幕內容。

A 字幕：
{a_text}

B 字幕：
{b_text}
"""
        raw = client.complete(system, user)
        data = json.loads(clean_json_text(raw))
        if isinstance(data, dict):
            data = data.get("matches", [])
        if not isinstance(data, list):
            raise RuntimeError("AI 回傳不是 JSON 陣列。")

        expected = {self.a_entries[i].number for i in a_ids}
        valid_b = {self.b_entries[i].number for i in b_ids}
        seen = set()
        valid_results = []
        invalid_results = []
        explicit_unmatched = []

        for pos, item in enumerate(data, 1):
            if not isinstance(item, dict):
                invalid_results.append({"position": pos, "reason": "不是 JSON 物件", "raw": repr(item)[:300]})
                continue
            try:
                a_num = int(item.get("a"))
            except Exception:
                invalid_results.append({"position": pos, "reason": "A 編號無效", "raw": item})
                continue
            if a_num not in expected:
                invalid_results.append({"position": pos, "a": a_num, "reason": "A 不在本次送出範圍"})
                continue
            if a_num in seen:
                invalid_results.append({"position": pos, "a": a_num, "reason": "同一 A 重複回傳"})
                continue
            seen.add(a_num)

            raw_b = item.get("b", [])
            if raw_b is None:
                raw_b = []
            if not isinstance(raw_b, list):
                invalid_results.append({"position": pos, "a": a_num, "reason": "b 不是陣列"})
                continue
            parsed_b = []
            bad_b = []
            for value in raw_b:
                try:
                    b_num = int(value)
                except Exception:
                    bad_b.append(value)
                    continue
                if b_num not in valid_b:
                    bad_b.append(value)
                else:
                    parsed_b.append(b_num)
            if bad_b:
                invalid_results.append({
                    "position": pos, "a": a_num,
                    "reason": "含有不在本次 B 範圍或格式無效的 B 編號",
                    "invalid_b": bad_b,
                })
            normalized = dict(item)
            normalized["a"] = a_num
            normalized["b"] = parsed_b
            valid_results.append(normalized)
            if not parsed_b:
                explicit_unmatched.append(a_num)

        unreturned = sorted(expected - seen)
        return {
            "results": valid_results,
            "valid_returned_a": sorted(seen),
            "explicit_unmatched_a": sorted(explicit_unmatched),
            "unreturned_a": unreturned,
            "invalid_results": invalid_results,
        }

    def apply_ai_results(self, results: List[Dict[str, Any]], final: bool = True) -> int:
        a_num_to_idx = {e.number: i for i, e in enumerate(self.a_entries)}
        b_num_to_idx = {e.number: i for i, e in enumerate(self.b_entries)}
        applied = 0
        for item in results:
            try:
                ai = a_num_to_idx[int(item["a"])]
                bis = [b_num_to_idx[int(x)] for x in item.get("b", []) if int(x) in b_num_to_idx]
                confidence = float(item.get("confidence", 0.5))
                note = str(item.get("note", ""))
            except Exception:
                continue
            old = self.matches.get(ai)
            if old and old.status.startswith("manual"): continue
            if old and old.confidence > confidence: continue
            self.matches[ai] = MatchInfo(ai, bis, "ai", confidence, note)
            applied += 1
        self.refresh_trees()
        self.schedule_autosave()
        if final:
            self.busy = False
            self.set_status(f"AI 配對完成，套用 {applied} 筆建議。")
        return applied

    def finish_ai_job(self, total_applied: int, total_chunks: int):
        self.busy = False

        if self.autosave_after_id is not None:
            try:
                self.after_cancel(self.autosave_after_id)
            except Exception:
                pass
            self.autosave_after_id = None
        self.autosave_now()

        suspicious = self._detect_suspicious_matches()
        self.ai_check_data["suspicious_matches"] = suspicious
        self.ai_check_data["status"] = "completed"
        self.ai_check_data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.ai_check_data["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save_ai_check()

        missing_count = len(self.ai_check_data.get("unreturned_a", []))
        invalid_count = len(self.ai_check_data.get("invalid_results", []))
        suspicious_count = len(suspicious)

        self.set_status(
            f"AI 配對完成：{total_chunks} 個分段｜套用 {total_applied} 筆｜"
            f"未回傳 {missing_count}｜格式異常 {invalid_count}｜疑似異常 {suspicious_count}"
        )

        try:
            self.bell()
        except Exception:
            pass
        self.show_ai_completion_dialog(total_chunks, total_applied)

    def ai_failed(self, error: str):
        self.busy = False
        self.set_status("AI 配對失敗。")
        messagebox.showerror("AI 配對失敗", error)

    def build_output(self) -> List[SrtEntry]:
        output = []
        used_b = set()
        for ai, a in enumerate(self.a_entries):
            m = self.matches.get(ai)
            if m and m.b_indices:
                valid = [bi for bi in m.b_indices if 0 <= bi < len(self.b_entries)]
                if not valid:
                    continue
                start = m.assigned_start_ms if m.assigned_start_ms is not None else min(self.b_entries[x].start_ms for x in valid)
                end = m.assigned_end_ms if m.assigned_end_ms is not None else max(self.b_entries[x].end_ms for x in valid)
                used_b.update(valid)
                output.append([start, end, a.text, "A", ai])
            elif self.keep_a_var.get():
                start = m.assigned_start_ms if m and m.assigned_start_ms is not None else a.start_ms
                end = m.assigned_end_ms if m and m.assigned_end_ms is not None else a.end_ms
                if end > start:
                    output.append([start, end, a.text, "A_UNMATCHED", ai])
        if self.keep_b_var.get():
            for bi, b in enumerate(self.b_entries):
                if bi not in used_b:
                    output.append([b.start_ms, b.end_ms, b.text, "B", bi])

        output.sort(key=lambda x: (x[0], 0 if x[3].startswith("A") else 1, x[1]))
        deduped, seen = [], set()
        for item in output:
            key = (item[0], item[1], item[2])
            if key not in seen:
                seen.add(key); deduped.append(item)

        # 保留原始時間，不自動截短重疊字幕。
        # 多人同時說話或其他編排可能刻意使用重疊時間；改由「檢查時間重疊」提示人工確認。
        result = []
        for start_ms, end_ms, text, _source, _idx in deduped:
            if end_ms <= start_ms:
                end_ms = start_ms + 1
            result.append(SrtEntry(len(result) + 1, start_ms, end_ms, text))
        return result

    def find_output_overlaps(self, output: Optional[List[SrtEntry]] = None):
        output = output if output is not None else self.build_output()
        overlaps = []
        active = []
        for i, current in enumerate(output):
            active = [(j, e) for j, e in active if e.end_ms > current.start_ms]
            for j, previous in active:
                if previous.end_ms > current.start_ms:
                    overlaps.append((j, previous, i, current))
            active.append((i, current))
        return overlaps

    def show_overlap_report(self):
        output = self.build_output()
        overlaps = self.find_output_overlaps(output)
        self.overlap_warning_count = len(overlaps)
        if not overlaps:
            messagebox.showinfo("時間重疊檢查", "目前最終輸出沒有時間重疊。")
            return
        preview = []
        for _, first, _, second in overlaps[:20]:
            preview.append(
                f"#{first.number} {first.start} → {first.end}\n"
                f"  {first.text.replace(chr(10), ' / ')}\n"
                f"與 #{second.number} {second.start} → {second.end}\n"
                f"  {second.text.replace(chr(10), ' / ')}"
            )
        more = f"\n\n另有 {len(overlaps)-20} 組未列出。" if len(overlaps) > 20 else ""
        messagebox.showwarning(
            "發現時間重疊",
            f"共發現 {len(overlaps)} 組重疊。程式不會自動修改時間，請判斷是否為預期的同時顯示，或需要手動修正。\n\n"
            + "\n\n".join(preview) + more
        )

    def export_srt(self):
        if not self.a_entries or not self.b_entries:
            messagebox.showwarning("尚未載入", "請先載入 A、B 字幕。")
            return
        output = self.build_output()
        if not output:
            messagebox.showwarning("沒有配對", "目前沒有可輸出的配對字幕。")
            return
        match_conflicts = self.find_match_conflicts()
        if match_conflicts:
            go_back = messagebox.askyesno(
                "輸出前發現配對衝突",
                f"目前有 {len(match_conflicts)} 組包覆型配對衝突。\n\n"
                "例如：A1→B1，但 A2→B1、B2。\n"
                "這類結構通常會造成輸出字幕重複或重疊。\n\n"
                "要先回去檢查嗎？\n"
                "選「是」跳到第一組；選「否」仍繼續檢查時間重疊並輸出。"
            )
            if go_back:
                self.match_conflict_cursor = 0
                self.select_match_conflict(match_conflicts[0])
                return

        overlaps = self.find_output_overlaps(output)
        if overlaps and not messagebox.askyesno(
            "輸出前發現時間重疊",
            f"最終字幕有 {len(overlaps)} 組時間重疊。\n"
            "可能是預期的多人同時說話、字幕同時顯示或其他編排，也可能需要修正。\n\n"
            "程式不會自動截短。仍要原樣輸出嗎？"
        ):
            self.show_overlap_report()
            return
        default = f"{Path(self.b_path).stem}_套用A人工字幕.srt" if self.b_path else "B_套用A人工字幕.srt"
        path = filedialog.asksaveasfilename(
            title="輸出新 SRT", defaultextension=".srt",
            initialfile=default, filetypes=[("SRT 字幕", "*.srt")]
        )
        if not path:
            return
        try:
            write_srt(path, output)
            self.export_report(Path(path).with_suffix(".配對報告.csv"))
            self.set_status(f"已輸出：{path}")
            messagebox.showinfo("完成", f"已輸出 {len(output)} 條字幕。\n\n{path}")
        except Exception as e:
            messagebox.showerror("輸出失敗", str(e))

    def export_report(self, path: Path):
        import csv
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["A編號", "B編號", "狀態", "信心", "備註", "A字幕"])
            for ai, a in enumerate(self.a_entries):
                m = self.matches.get(ai)
                if not m:
                    w.writerow([a.number, "", "未處理", "", "", a.text.replace("\n", " / ")])
                else:
                    bnums = ",".join(str(self.b_entries[x].number) for x in m.b_indices if x < len(self.b_entries))
                    w.writerow([a.number, bnums, m.status, m.confidence, m.note, a.text.replace("\n", " / ")])

    def save_project(self):
        if not self.a_path or not self.b_path:
            messagebox.showwarning("尚未載入", "請先載入 A、B 字幕。")
            return
        path = filedialog.asksaveasfilename(
            title="儲存配對專案", defaultextension=".json",
            initialfile="字幕配對專案.json", filetypes=[("JSON 專案", "*.json")]
        )
        if not path:
            return
        data = {
            "version": APP_VERSION,
            "a_path": self.a_path,
            "b_path": self.b_path,
            "media_path": self.media_path,
            "a_entries": [asdict(e) for e in self.a_entries],
            "b_entries": [asdict(e) for e in self.b_entries],
            "matches": {str(k): asdict(v) for k, v in self.matches.items()},
            "b_unique": sorted(self.b_unique),
            "keep_unmatched_a": self.keep_a_var.get(),
            "keep_unmatched_b": self.keep_b_var.get(),
        }
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.set_status(f"專案已儲存：{path}")

    def load_project(self):
        path = filedialog.askopenfilename(title="開啟配對專案", filetypes=[("JSON 專案", "*.json")])
        if not path: return
        try:
            self.restore_from_data(json.loads(Path(path).read_text(encoding="utf-8")))
            self.set_status(f"專案已開啟：{path}")
        except Exception as e:
            messagebox.showerror("開啟失敗", str(e))

    def set_status(self, text: str):
        self.status_var.set(text)
        self.update_idletasks()

    def on_close(self):
        if self.busy and self.ai_check_data:
            self.ai_check_data["status"] = "closed_during_ai"
            self.ai_check_data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.save_ai_check()
        try:
            if self.vlc_player is not None:
                self.vlc_player.stop()
                self.vlc_player.release()
        except Exception:
            pass
        try:
            if self.vlc_instance is not None:
                self.vlc_instance.release()
        except Exception:
            pass
        try:
            if self.vlc_dll_dir_handle is not None:
                self.vlc_dll_dir_handle.close()
        except Exception:
            pass
        self.autosave_now()
        self.config_mgr.data["window_geometry"] = self.geometry()
        self.config_mgr.data["keep_unmatched_a"] = self.keep_a_var.get()
        self.config_mgr.data["keep_unmatched_b"] = self.keep_b_var.get()
        self.config_mgr.data["active_profile"] = self.active_profile_var.get()
        self.config_mgr.save()
        self.destroy()


def main():
    app = SubtitleMatcherApp()

    def report_callback_exception(exc_type, exc_value, exc_traceback):
        import traceback
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        try:
            log_path = LOG_DIR / "callback_error.log"
            log_path.write_text(details, encoding="utf-8")
        except Exception:
            pass
        messagebox.showerror(
            "程式發生錯誤",
            "已攔截介面事件錯誤，詳細內容已寫入 callback_error.log。\n\n"
            + details[-2500:]
        )

    app.report_callback_exception = report_callback_exception
    app.mainloop()


if __name__ == "__main__":
    main()
