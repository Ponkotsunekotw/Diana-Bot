import discord
from discord import app_commands
from discord.ext import commands, tasks
import re
import json
import os
import sys
import base64
import asyncio
import chromadb
import shutil
import random
import mimetypes
import difflib
import io
from datetime import datetime, timedelta
from chromadb.utils import embedding_functions
from openai import AsyncOpenAI
from image_gen import generate_action_image, ImageGenError, suggest_filename
from gui_runtime import update_gui_status
import diana_addons

# ---------------------------------------------------------
# 0. 快速配置區 / 資料夾結構
# ---------------------------------------------------------
# 目錄結構說明：
#   config/   -> 密鑰與人設等設定檔 (discord_token.txt, google API key, character_persona.json)
#   data/     -> 執行期狀態 json (bot_state, memory, favorability, reminders, user_profile, version, base)
#   logs/     -> 成長日誌 log.txt (使用中) ；logs/done/ 存放已推播歸檔的日誌
#   updates/  -> 功能許願池 upgrade.txt (使用中) ；updates/done/ 存放已被消化的許願清單
#   diana_memory_db/ -> 向量記憶庫 (不搬動)
#   old/      -> 由既有自動升級腳本管理的 bot.py / character_persona.json 歷史版本備份 (維持原樣，不搬動)
CONFIG_DIR = "config"
DATA_DIR = "data"
LOG_DIR = "logs"
LOG_DONE_DIR = os.path.join(LOG_DIR, "done")
UPDATE_DIR = "updates"
UPDATE_DONE_DIR = os.path.join(UPDATE_DIR, "done")
LEGACY_OLD_DIR = "old"  # 既有版本備份資料夾，維持原樣不搬動
IMG_DIR = "img"  # 動作圖片庫：存放可替換文字動作描述的示意圖
AUTO_DELETE_SECONDS = 60  # 大部分指令的狀態訊息，發送後幾秒自動刪除，避免洗版

def ensure_directories():
    for d in (CONFIG_DIR, DATA_DIR, LOG_DIR, LOG_DONE_DIR, UPDATE_DIR, UPDATE_DONE_DIR, IMG_DIR):
        os.makedirs(d, exist_ok=True)

def migrate_legacy_file(old_path, new_path):
    """向後相容：若舊版根目錄仍有散落檔案，第一次啟動時自動搬進新資料夾結構。"""
    if os.path.exists(old_path) and not os.path.exists(new_path):
        try:
            shutil.move(old_path, new_path)
            print(f"📦 [結構遷移] {old_path} → {new_path}")
        except Exception as e:
            print(f"⚠️ [結構遷移失敗] {old_path} → {new_path}: {e}")

def migrate_legacy_layout():
    ensure_directories()
    legacy_map = {
        "discord_token.txt": os.path.join(CONFIG_DIR, "discord_token.txt"),
        "google API key -2.txt": os.path.join(CONFIG_DIR, "google_api_key_chat.txt"),
        "character_persona.json": os.path.join(CONFIG_DIR, "character_persona.json"),
        "base.json": os.path.join(DATA_DIR, "base.json"),
        "bot_state.json": os.path.join(DATA_DIR, "bot_state.json"),
        "favorability.json": os.path.join(DATA_DIR, "favorability.json"),
        "memory.json": os.path.join(DATA_DIR, "memory.json"),
        "reminders.json": os.path.join(DATA_DIR, "reminders.json"),
        "user_profile.json": os.path.join(DATA_DIR, "user_profile.json"),
        "version.json": os.path.join(DATA_DIR, "version.json"),
        "log.txt": os.path.join(LOG_DIR, "log.txt"),
        "upgrade.txt": os.path.join(UPDATE_DIR, "upgrade.txt"),
    }
    for old_path, new_path in legacy_map.items():
        migrate_legacy_file(old_path, new_path)

    # 額外相容：如果你先前已經照舊指示把金鑰放到 config/google API key -2.txt，
    # 這裡也一併搬到新的專用聊天金鑰檔名，並只取第一行（金鑰），避免殘留的第二行模型名稱污染。
    old_config_key_path = os.path.join(CONFIG_DIR, "google API key -2.txt")
    chat_key_path = os.path.join(CONFIG_DIR, "google_api_key_chat.txt")
    if os.path.exists(old_config_key_path) and not os.path.exists(chat_key_path):
        try:
            with open(old_config_key_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
            with open(chat_key_path, "w", encoding="utf-8") as f:
                f.write(first_line + "\n")
            os.remove(old_config_key_path)
            print(f"📦 [結構遷移] {old_config_key_path} → {chat_key_path}（僅取第一行金鑰，已與演化引擎金鑰分離）")
        except Exception as e:
            print(f"⚠️ [金鑰遷移失敗] {e}")

migrate_legacy_layout()

TOKEN_FILE = os.path.join(CONFIG_DIR, "discord_token.txt")
# 聊天端專用金鑰檔，跟 diana_evolution.py 的金鑰檔完全分開存放，
# 兩支程式互不讀寫對方的檔案，避免搶檔或格式不一致造成的認證錯誤。
GOOGLE_KEY_FILE = os.path.join(CONFIG_DIR, "google_api_key_chat.txt")
GUI_CONFIG_FILE = os.path.join(CONFIG_DIR, "gui_config.json")

def _load_gui_config():
    try:
        with open(GUI_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_gui_config_updates(updates: dict):
    """將設定變更合併寫回 config/gui_config.json，確保指令切換的模型重啟後仍會保留。"""
    try:
        cfg = _load_gui_config()
        cfg.update(updates)
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(GUI_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 儲存 gui_config.json 失敗: {e}")

_gui_cfg = _load_gui_config()
LOCAL_DEFAULT_MODEL = _gui_cfg.get("local_model", "google/gemma-4-e2b")
MODEL_NAME = _gui_cfg.get("master_model") or LOCAL_DEFAULT_MODEL
GUEST_MODEL_NAME = _gui_cfg.get("guest_model") or LOCAL_DEFAULT_MODEL
MASTER_BACKEND = _gui_cfg.get("master_backend", "")
GUEST_BACKEND = _gui_cfg.get("guest_backend", "")
CHAT_TEMPERATURE = float(_gui_cfg.get("temperature", 0.75))
MAX_THINKING_TOKENS = int(_gui_cfg.get("thinking_tokens", 256))
LOCAL_BASE_URL = _gui_cfg.get("local_base_url", "http://127.0.0.1:1234/v1")
WEB_BASE_URL = _gui_cfg.get("web_base_url", "https://generativelanguage.googleapis.com/v1beta/openai/")
WEB_MODEL_NAME = _gui_cfg.get("web_model", "gemini-3.5-flash-lite")
_last_gui_cfg_mtime = os.path.getmtime(GUI_CONFIG_FILE) if os.path.exists(GUI_CONFIG_FILE) else 0.0

def get_discord_token():
    if not os.path.exists(TOKEN_FILE):
        print(f"❌ 錯誤：找不到 '{TOKEN_FILE}'！請建立該檔案並貼入 Discord Bot Token。")
        sys.exit(1)
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        token = f.read().strip()
    if not token:
        print(f"❌ 錯誤：'{TOKEN_FILE}' 內容為空！")
        sys.exit(1)
    return token

DISCORD_TOKEN = get_discord_token()

# ---------------------------------------------------------
# 1. API 與 ChromaDB 初始化
# ---------------------------------------------------------
client = AsyncOpenAI(
    base_url=LOCAL_BASE_URL,
    api_key="lm-studio",
    timeout=120.0
)

def get_google_api_key():
    """
    聊天端專用金鑰讀取。只取檔案第一行 —— 就算檔案裡不小心混進第二行內容
    （例如誤把演化引擎的金鑰檔複製過來），也不會把整個檔案內容當成 api_key
    （舊版用 f.read().strip() 讀整檔，若檔案是「金鑰\n模型名稱」兩行格式會直接認證失敗）。
    """
    if os.path.exists(GOOGLE_KEY_FILE):
        with open(GOOGLE_KEY_FILE, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
        return first_line or None
    return None

google_api_key = get_google_api_key()
google_client = None
if google_api_key:
    google_client = AsyncOpenAI(
        base_url=WEB_BASE_URL,
        api_key=google_api_key,
        timeout=120.0
    )

def sync_runtime_config_from_gui():
    """若 GUI 快速切換了主人/訪客推理引擎或模型，動態熱載入最新設定，無須重啟 Bot。"""
    global LOCAL_DEFAULT_MODEL, MODEL_NAME, GUEST_MODEL_NAME, MASTER_BACKEND, GUEST_BACKEND
    global CHAT_TEMPERATURE, MAX_THINKING_TOKENS, LOCAL_BASE_URL, WEB_BASE_URL, WEB_MODEL_NAME
    global client, google_client, google_api_key, _last_gui_cfg_mtime
    try:
        if not os.path.exists(GUI_CONFIG_FILE):
            return
        mtime = os.path.getmtime(GUI_CONFIG_FILE)
        if mtime <= _last_gui_cfg_mtime:
            return
        _last_gui_cfg_mtime = mtime
        cfg = _load_gui_config()
        LOCAL_DEFAULT_MODEL = cfg.get("local_model", LOCAL_DEFAULT_MODEL)
        MODEL_NAME = cfg.get("master_model") or LOCAL_DEFAULT_MODEL
        GUEST_MODEL_NAME = cfg.get("guest_model") or LOCAL_DEFAULT_MODEL
        MASTER_BACKEND = cfg.get("master_backend", MASTER_BACKEND)
        GUEST_BACKEND = cfg.get("guest_backend", GUEST_BACKEND)
        CHAT_TEMPERATURE = float(cfg.get("temperature", CHAT_TEMPERATURE))
        MAX_THINKING_TOKENS = int(cfg.get("thinking_tokens", MAX_THINKING_TOKENS))
        new_local_url = cfg.get("local_base_url", LOCAL_BASE_URL)
        new_web_url = cfg.get("web_base_url", WEB_BASE_URL)
        WEB_MODEL_NAME = cfg.get("web_model", WEB_MODEL_NAME)
        if new_local_url != LOCAL_BASE_URL:
            LOCAL_BASE_URL = new_local_url
            client = AsyncOpenAI(base_url=LOCAL_BASE_URL, api_key="lm-studio", timeout=120.0)
        latest_key = get_google_api_key()
        if new_web_url != WEB_BASE_URL or latest_key != google_api_key:
            WEB_BASE_URL = new_web_url
            google_api_key = latest_key
            if google_api_key:
                google_client = AsyncOpenAI(base_url=WEB_BASE_URL, api_key=google_api_key, timeout=120.0)
    except Exception as e:
        print(f"⚠️ 同步 gui_config.json 時發生錯誤: {e}")

def is_web_model_or_backend(model: str, explicit_backend: str = "") -> bool:
    if explicit_backend in ("web", "local"):
        return explicit_backend == "web"
    return (model == WEB_MODEL_NAME or "gemini" in model.lower())

async def call_llm(model, messages, temperature, max_thinking_tokens=None, backend=""):
    sync_runtime_config_from_gui()
    if is_web_model_or_backend(model, backend):
        if not google_client:
            latest_key = get_google_api_key()
            if latest_key:
                global google_api_key
                google_api_key = latest_key
                google_client = AsyncOpenAI(base_url=WEB_BASE_URL, api_key=google_api_key, timeout=120.0)
            else:
                raise Exception(f"未載入 Web API Key，請檢查 '{GOOGLE_KEY_FILE}' 檔案是否正確！")
        return await google_client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature
        )
    else:
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature
        }
        if max_thinking_tokens:
            kwargs["extra_body"] = {"max_thinking_tokens": max_thinking_tokens}
        return await client.chat.completions.create(**kwargs)

async def call_llm_with_retry(model, messages, temperature, max_thinking_tokens=None, retries=2, backoff_seconds=1.0, backend=""):
    """
    針對『閒置一段時間後、長連線 keep-alive 失效導致第一次請求 Connection error』
    這種暫時性錯誤做輕量重試，安靜重試成功就不會讓使用者看到錯誤訊息；
    只有重試全部失敗才會真的往外拋出例外，交由呼叫端顯示錯誤給使用者。
    """
    last_exception = None
    for attempt in range(1, retries + 2):  # 共嘗試 retries+1 次
        try:
            return await call_llm(model, messages, temperature, max_thinking_tokens, backend=backend)
        except Exception as e:
            last_exception = e
            if attempt <= retries:
                print(f"⚠️ [呼叫重試 {attempt}/{retries}] {model} 發生暫時性錯誤: {e}，{backoff_seconds}秒後重試...")
                await asyncio.sleep(backoff_seconds)
            else:
                raise last_exception

chroma_client = chromadb.PersistentClient(path="./diana_memory_db")
emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
    model_name="paraphrase-multilingual-MiniLM-L12-v2"
)
collection = chroma_client.get_or_create_collection(
    name="diana_memories", 
    embedding_function=emb_fn
)

# ---------------------------------------------------------
# 2. 數據庫與檔案管理
# ---------------------------------------------------------
PROFILE_FILE = os.path.join(DATA_DIR, "user_profile.json")
FAVOR_FILE = os.path.join(DATA_DIR, "favorability.json")
GUEST_NOTES_FILE = os.path.join(DATA_DIR, "guest_notes.json")
REMINDER_FILE = os.path.join(DATA_DIR, "reminders.json")
MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")
STATE_FILE = os.path.join(DATA_DIR, "bot_state.json")
PERSONA_FILE = os.path.join(CONFIG_DIR, "character_persona.json")
VERSION_FILE = os.path.join(DATA_DIR, "version.json")
BASE_FILE = os.path.join(DATA_DIR, "base.json")
LOG_FILE = os.path.join(LOG_DIR, "log.txt")
UPGRADE_FILE = os.path.join(UPDATE_DIR, "upgrade.txt")
RESTART_FLAG_FILE = os.path.join(DATA_DIR, "pending_restart.flag")
MAX_SHORT_TERM = 8

def load_json(filepath, default_val):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default_val
    return default_val

def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

async def load_json_async(filepath, default_val):
    return await asyncio.to_thread(load_json, filepath, default_val)

async def save_json_async(filepath, data):
    await asyncio.to_thread(save_json, filepath, data)

# ---------------------------------------------------------
# 0.5 動作插圖系統：把常見的 *(動作描述)* 文字替換成對應的圖片
# ---------------------------------------------------------
# 設定檔：config/action_images.json，格式為一個陣列，每筆是：
#   {"keywords": ["拉拉主人的袖子", "..."], "file": "img/裡的檔名.png"}
# 只要文字裡的 *(...)* 片段命中任一關鍵字，就會把該片段換成對應圖片。
# 之後要新增動作圖，只要把圖丟進 img/ 資料夾，再到這個 json 裡加一筆設定即可，不用改程式碼。
ACTION_IMAGE_CONFIG_FILE = os.path.join(CONFIG_DIR, "action_images.json")
ACTION_TEXT_PATTERN = re.compile(r'[\*＊]\(([^)]+)\)[\*＊]')

def _default_action_image_map():
    """三個示範動作：拉袖子 / 早晨揉眼睛 / 歪頭想了想，對應 img/ 資料夾裡實際存在的示範圖片。"""
    return [
        {
            "name": "拉拉主人的袖子",
            "keywords": ["拉拉主人的袖子", "拉主人的袖子", "拉拉你的袖子", "扯扯主人的衣角", "拉住主人的袖子"],
            "file": "action_pull_sleeve.png"
        },
        {
            "name": "早晨揉眼睛",
            "keywords": ["揉揉眼睛", "揉眼睛", "揉了揉眼睛"],
            "file": "action_morning_rub_eyes.jpg"
        },
        {
            "name": "歪頭想了想",
            "keywords": ["歪頭想了想", "歪頭想", "歪頭思考", "歪著頭想"],
            "file": "action_head_tilt_thinking.jpg"
        }
    ]

def load_action_image_map():
    """讀取動作圖片對照表，檔案不存在時會自動建立一份含三個示範動作的預設設定。"""
    if not os.path.exists(ACTION_IMAGE_CONFIG_FILE):
        default_map = _default_action_image_map()
        save_json(ACTION_IMAGE_CONFIG_FILE, default_map)
        return default_map
    return load_json(ACTION_IMAGE_CONFIG_FILE, _default_action_image_map())

# --- 模糊比對：用「語意相似度」判斷兩段動作描述是不是在講同一件事 ---
# 直接沿用上面 RAG 記憶系統已經在用的同一顆多語言 embedding 模型 (emb_fn)，
# 而不是單純比對字面是否相同/相似——因為像「歪頭想了想」跟「歪頭思考了一下」
# 這種同義詞替換（想 vs 思考），字面比對（子字串、字元相似度）幾乎抓不到，
# 但語意向量會知道「想」跟「思考」意思很接近。
#
# FUZZY_MATCH_THRESHOLD 是 cosine similarity 的門檻值（0~1，越高越嚴格）：
#   - 完全比對（子字串包含）永遠優先命中，不受這個門檻影響。
#   - 語意相似度 >= 門檻值，才會被視為模糊命中。
#   - 依經驗，paraphrase-multilingual-MiniLM-L12-v2 這顆模型對「同一件事的不同講法」
#     通常落在 0.55~0.8 之間，完全無關的句子多半在 0.3 以下，所以先抓 0.55 當預設值。
#     如果你發現常常抓不到該中的動作，可以調低一點 (例如 0.5)；
#     如果發現常常誤判到不相關的動作，就調高一點 (例如 0.6~0.65)。
FUZZY_MATCH_THRESHOLD = 0.55

_action_embedding_cache = {}

def _get_text_embedding(text):
    """算一段文字的 embedding 向量，同一段文字只會在程式運行期間算一次（有快取）。"""
    if text not in _action_embedding_cache:
        _action_embedding_cache[text] = emb_fn([text])[0]
    return _action_embedding_cache[text]

def _cosine_similarity(vec_a, vec_b):
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = sum(a * a for a in vec_a) ** 0.5
    norm_b = sum(b * b for b in vec_b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

def _best_match_in_map(action_text, action_map):
    """
    在整個動作對照表裡，找出跟 action_text 最相似的一筆設定。
    回傳 (entry 或 None, 相似度分數 0~1)。
    比對規則（由嚴到鬆）：
      1. 完全一致，或其中一段是另一段的子字串 → 直接視為滿分命中 (1.0)，不用算 embedding。
      2. 都不成立時，改用語意向量算 cosine similarity，取全部關鍵字中分數最高的那個。
    """
    best_entry, best_score = None, 0.0
    for entry in action_map:
        for kw in entry.get("keywords", []):
            if not kw:
                continue
            if kw in action_text or action_text in kw:
                return entry, 1.0
            score = _cosine_similarity(_get_text_embedding(kw), _get_text_embedding(action_text))
            if score > best_score:
                best_entry, best_score = entry, score
    return best_entry, best_score

def find_action_image(text):
    """
    掃描文字裡所有 *(動作描述)* 片段，回傳第一個模糊比對命中的結果：
    (圖片完整路徑, 命中的完整片段字串 例如 '*(拉拉主人的袖子)*')。
    找不到對應圖片（或圖檔實際不存在，或相似度不到門檻）就回傳 (None, None)。
    一則訊息只替換第一個命中的片段，避免一次洗出太多圖片。
    """
    action_map = load_action_image_map()
    for match in ACTION_TEXT_PATTERN.finditer(text):
        action_text = match.group(1)
        entry, score = _best_match_in_map(action_text, action_map)
        if entry and score >= FUZZY_MATCH_THRESHOLD:
            img_path = os.path.join(IMG_DIR, entry.get("file", ""))
            if os.path.exists(img_path):
                return img_path, match.group(0)
    return None, None

def find_unmatched_action(text):
    """
    找出文字中第一個 *(動作描述)* 片段的內容，但前提是這個片段『目前沒有』
    對應到任何已設定的動作圖片（用跟 find_action_image 一樣的模糊比對規則判斷）。
    用在聊天回覆送出後，判斷要不要跳出『要不要生成插圖』的按鈕詢問。
    完全沒有動作片段，或片段已經有圖了，都回傳 None。
    """
    match = ACTION_TEXT_PATTERN.search(text)
    if not match:
        return None
    action_text = match.group(1)
    img_path, _ = find_action_image(text)
    if img_path:
        return None
    return action_text

# --- 「不用了」清單：記錄主人按過拒絕生成的動作，避免一直重複詢問同一個動作 ---
ACTION_IMAGE_DECLINED_FILE = os.path.join(CONFIG_DIR, "action_image_declined.json")

def load_declined_actions():
    return load_json(ACTION_IMAGE_DECLINED_FILE, [])

def add_declined_action(action_text):
    declined = load_declined_actions()
    if action_text not in declined:
        declined.append(action_text)
        save_json(ACTION_IMAGE_DECLINED_FILE, declined)

def is_action_declined(action_text):
    """用跟找圖一樣的語意模糊比對規則，判斷這個動作是不是主人之前按過『不用了』。"""
    declined = load_declined_actions()
    for d in declined:
        if not d:
            continue
        if d in action_text or action_text in d:
            return True
        if _cosine_similarity(_get_text_embedding(d), _get_text_embedding(action_text)) >= FUZZY_MATCH_THRESHOLD:
            return True
    return False

def prepare_action_message(text):
    """
    把文字中命中的動作片段抽掉，並準備好對應的 discord.File。
    回傳 (剩餘文字, discord.File 或 None)。
    """
    img_path, full_match = find_action_image(text)
    if not img_path:
        return text, None
    remaining = text.replace(full_match, "", 1)
    remaining = re.sub(r'\n{3,}', '\n\n', remaining).strip()
    return remaining, discord.File(img_path)

async def send_action_message(channel, text, **kwargs):
    """
    黛安娜發送一般頻道訊息的統一入口：文字裡如果有命中的動作描述（例如 *(拉拉主人的袖子)*），
    會自動換成對應圖片一起送出；沒命中就照原樣純文字發送。
    """
    remaining, image_file = prepare_action_message(text)
    if image_file:
        if remaining:
            return await channel.send(remaining, file=image_file, **kwargs)
        return await channel.send(file=image_file, **kwargs)
    return await channel.send(text, **kwargs)

async def send_action_interaction(interaction, text, **kwargs):
    """同 send_action_message，但用在 slash command 的 interaction 回覆，並套用指令回覆自動清版面機制。"""
    remaining, image_file = prepare_action_message(text)
    if image_file:
        if remaining:
            return await temp_reply(interaction, remaining, file=image_file, **kwargs)
        return await temp_reply(interaction, file=image_file, **kwargs)
    return await temp_reply(interaction, text, **kwargs)

def guess_image_ext(mime_type):
    ext = mimetypes.guess_extension(mime_type) or ".png"
    if ext == ".jpe":
        ext = ".jpg"
    return ext

def save_new_action_image(action_text, image_bytes, mime_type, extra_keywords=None):
    """
    把生成好的圖片 bytes 存檔到 img/，並在 config/action_images.json 裡新增一筆對照設定。
    回傳 (save_path, filename, keyword_list)。
    共用給 /generate_action_image 指令、以及聊天中『偵測到新動作 → 按鈕確認生成』的流程使用。
    """
    ext = guess_image_ext(mime_type)
    filename = suggest_filename(action_text, ext)
    save_path = os.path.join(IMG_DIR, filename)
    with open(save_path, "wb") as f:
        f.write(image_bytes)

    keyword_list = [kw.strip() for kw in (extra_keywords or []) if kw.strip()]
    if action_text not in keyword_list:
        keyword_list.insert(0, action_text)

    action_map = load_action_image_map()
    action_map.append({
        "name": action_text,
        "keywords": keyword_list,
        "file": filename
    })
    save_json(ACTION_IMAGE_CONFIG_FILE, action_map)
    return save_path, filename, keyword_list

# --- 指令回覆自動清版面：大部分指令的回覆訊息，過一段時間會自動刪除 ---
# 例外：像 /update、/restart 這種『訊息送出沒幾秒後整個程式就會重啟』的指令，
# 排程刪除在程式重啟後也不會真的執行到，所以那幾個指令保留原本的 interaction.response 呼叫，不套用這個機制。
COMMAND_MSG_TTL = 30  # 秒數，可依你的使用習慣調整，越短版面越乾淨，但太短會來不及看內容。

async def temp_reply(interaction: discord.Interaction, content=None, *, ephemeral=False, ttl=COMMAND_MSG_TTL, **kwargs):
    """
    取代 interaction.response.send_message：訊息會在 ttl 秒後自動刪除，避免指令回覆長期洗版。
    ephemeral 訊息本來就只有本人看得到、會自動被 Discord 收起來，所以不會額外排程刪除。
    """
    await interaction.response.send_message(content, ephemeral=ephemeral, **kwargs)
    if not ephemeral and ttl:
        try:
            msg = await interaction.original_response()
            await msg.delete(delay=ttl)
        except discord.HTTPException:
            pass

async def temp_followup(interaction: discord.Interaction, content=None, *, ephemeral=False, ttl=COMMAND_MSG_TTL, **kwargs):
    """取代 interaction.followup.send：同樣是 ttl 秒後自動刪除（ephemeral 例外）。"""
    msg = await interaction.followup.send(content, ephemeral=ephemeral, **kwargs)
    if not ephemeral and ttl and msg:
        try:
            await msg.delete(delay=ttl)
        except discord.HTTPException:
            pass
    return msg

def _is_button_user_allowed(interaction: discord.Interaction) -> bool:
    """權限規則：必須已設定主人（/user），且只有主人能操作這些按鈕。"""
    master_id = get_master_id()
    if not master_id:
        return False
    return str(interaction.user.id) == str(master_id)

class ActionImagePreviewView(discord.ui.View):
    """
    生成完插圖後的『保留 / 重置 / 取消』確認畫面。
    在使用者按下『保留』之前，圖片只存在記憶體 (self.image_bytes) 裡，不會寫進 img/ 資料夾，
    按『取消』或逾時的話就直接丟棄，不需要額外清理暫存檔。
    """
    def __init__(self, action_text, image_bytes, mime_type, extra_notes=""):
        super().__init__(timeout=300)
        self.action_text = action_text
        self.image_bytes = image_bytes
        self.mime_type = mime_type
        self.extra_notes = extra_notes

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _is_button_user_allowed(interaction):
            await interaction.response.send_message("❌ 只有主人可以決定這張圖要不要保留喔！", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if getattr(self, "message", None):
            try:
                await self.message.edit(content=f"⌛ 插圖預覽逾時未處理，已自動取消。動作：『{self.action_text}』", attachments=[], view=self)
            except discord.HTTPException:
                pass

    def _make_file(self):
        ext = guess_image_ext(self.mime_type)
        return discord.File(io.BytesIO(self.image_bytes), filename=f"preview{ext}")

    @discord.ui.button(label="✅ 保留", style=discord.ButtonStyle.success)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        try:
            save_path, filename, keyword_list = save_new_action_image(
                self.action_text, self.image_bytes, self.mime_type
            )
        except Exception as e:
            await interaction.response.edit_message(
                content=f"❌ 保留時存檔失敗：{e}", attachments=[], view=self
            )
            self.stop()
            return

        await interaction.response.edit_message(
            content=(
                f"✅ 已加入動作圖庫！\n"
                f"📌 動作：『{self.action_text}』\n"
                f"🔑 關鍵字：{'、'.join(keyword_list)}\n"
                f"🖼️ 檔案：`img/{filename}`\n"
                f"以後聊天出現類似的動作描述，就會自動換成這張圖。"
            ),
            attachments=[self._make_file()],
            view=self
        )
        self.stop()

    @discord.ui.button(label="🔄 重置", style=discord.ButtonStyle.primary)
    async def regenerate(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=f"🔄 重新生成中……『{self.action_text}』", attachments=[], view=None)
        try:
            image_bytes, mime_type = await generate_action_image(self.action_text, self.extra_notes)
        except ImageGenError as e:
            await interaction.edit_original_response(content=f"❌ 重新生成失敗：{e}", view=None)
            self.stop()
            return
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ 重新生成時發生未預期的錯誤：{e}", view=None)
            self.stop()
            return

        self.image_bytes = image_bytes
        self.mime_type = mime_type
        for child in self.children:
            child.disabled = False
        await interaction.edit_original_response(
            content=f"🖼️ 新版本生成完成，要保留這張、還是再重置一次？\n📌 動作：『{self.action_text}』",
            attachments=[self._make_file()],
            view=self
        )

    @discord.ui.button(label="❌ 取消", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        add_declined_action(self.action_text)
        await interaction.response.edit_message(
            content=f"好，這張就不用了，黛安娜之後也不會再問『{self.action_text}』這個動作了。",
            attachments=[],
            view=self
        )
        self.stop()

class ActionImageOfferView(discord.ui.View):
    """聊天回覆裡出現『沒有對應圖片的新動作』時，跳出來詢問要不要生成插圖的按鈕。"""
    def __init__(self, action_text):
        super().__init__(timeout=180)
        self.action_text = action_text

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _is_button_user_allowed(interaction):
            await interaction.response.send_message("❌ 只有主人可以決定要不要生成插圖喔！", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if getattr(self, "message", None):
            try:
                await self.message.edit(content=f"⌛ 這則詢問已經逾時，黛安娜就先不生成『{self.action_text}』的插圖了。", view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="🎨 生成插圖", style=discord.ButtonStyle.success)
    async def generate(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"🎨 生成中……『{self.action_text}』", view=self)

        try:
            image_bytes, mime_type = await generate_action_image(self.action_text)
        except ImageGenError as e:
            await interaction.edit_original_response(content=f"❌ 生圖失敗：{e}", view=None)
            self.stop()
            return
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ 生圖時發生未預期的錯誤：{e}", view=None)
            self.stop()
            return

        preview_view = ActionImagePreviewView(self.action_text, image_bytes, mime_type)
        preview_msg = await interaction.edit_original_response(
            content=f"🖼️ 生成完成，要保留這張嗎？\n📌 動作：『{self.action_text}』",
            attachments=[preview_view._make_file()],
            view=preview_view
        )
        preview_view.message = preview_msg
        self.stop()

    @discord.ui.button(label="不用了", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        add_declined_action(self.action_text)
        await interaction.response.edit_message(
            content=f"好，黛安娜之後也不會再問『{self.action_text}』這個動作的插圖了。",
            view=self
        )
        self.stop()

async def offer_generate_action_image(channel, action_text):
    """在頻道裡跳出『要不要幫這個新動作生成插圖』的詢問。"""
    view = ActionImageOfferView(action_text)
    msg = await channel.send(
        f"💡 主人剛才的訊息裡有一個黛安娜還沒有對應插圖的動作：『{action_text}』，要幫她生成一張嗎？",
        view=view
    )
    view.message = msg
    return msg

def get_admin_password():
    base_data = load_json(BASE_FILE, {"admin_password": "0000"})
    return base_data.get("admin_password", "0000")

def set_admin_password(new_password):
    base_data = load_json(BASE_FILE, {"admin_password": "0000"})
    base_data["admin_password"] = new_password
    save_json(BASE_FILE, base_data)

def get_master_id():
    base_data = load_json(BASE_FILE, {"admin_password": "0000"})
    return base_data.get("master_id")

def set_master_id(user_id):
    base_data = load_json(BASE_FILE, {"admin_password": "0000"})
    base_data["master_id"] = str(user_id)
    save_json(BASE_FILE, base_data)

def load_versions():
    default_vers = {"bot": "1.0", "diana_addons": "1.0", "character_persona": "1.0"}
    vers = load_json(VERSION_FILE, default_vers)
    if "diana_addons" not in vers:
        vers["diana_addons"] = vers.get("bot", "1.0")
    return vers

def save_versions(vers):
    save_json(VERSION_FILE, vers)

_file_locks = {}

def get_file_lock(filepath):
    if filepath not in _file_locks:
        _file_locks[filepath] = asyncio.Lock()
    return _file_locks[filepath]

async def update_state_async(mutate_fn):
    """
    對 STATE_FILE 進行『讀取 -> 修改 -> 寫回』的原子操作，避免 on_message 與
    background_maintenance_task 同時讀寫 bot_state.json 造成互相覆蓋。
    mutate_fn(state_dict) 會直接原地修改傳入的 dict，回傳值會被忽略。
    回傳修改後的最新 state。
    """
    async with get_file_lock(STATE_FILE):
        state = await load_json_async(STATE_FILE, {})
        mutate_fn(state)
        await save_json_async(STATE_FILE, state)
        return state

def is_channel_ignored(channel_id, state):
    return str(channel_id) in state.get("ignored_channels", [])

def archive_done_file(source_path, done_dir, now=None):
    """
    將『使用中』的檔案 (如 upgrade.txt / log.txt) 歸檔為統一格式：done{YYMMDD}.txt
    若同一天已有同名歸檔，會自動加上 _2 / _3 ... 尾碼避免覆蓋。
    成功歸檔後回傳新檔案路徑；來源檔不存在或內容為空則回傳 None。
    """
    if not os.path.exists(source_path):
        return None
    try:
        with open(source_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except Exception:
        content = None
    if not content:
        return None

    now = now or datetime.now()
    date_str = now.strftime("%y%m%d")
    os.makedirs(done_dir, exist_ok=True)

    base_name = f"done{date_str}.txt"
    dest_path = os.path.join(done_dir, base_name)
    seq = 2
    while os.path.exists(dest_path):
        dest_path = os.path.join(done_dir, f"done{date_str}_{seq}.txt")
        seq += 1

    shutil.move(source_path, dest_path)
    return dest_path

def get_favorability(channel_id):
    favs = load_json(FAVOR_FILE, {})
    return favs.get(channel_id, 50)

async def update_favorability_async(channel_id, delta):
    async with get_file_lock(FAVOR_FILE):
        favs = await load_json_async(FAVOR_FILE, {})
        current = favs.get(channel_id, 50)
        new_val = max(0, min(100, current + delta))
        favs[channel_id] = new_val
        await save_json_async(FAVOR_FILE, favs)
        return new_val

def load_reminders():
    return load_json(REMINDER_FILE, [])

def save_reminders(reminders):
    save_json(REMINDER_FILE, reminders)

def normalize_persona_structure(raw_persona: dict) -> dict:
    """
    將 character_persona.json 正規化為『基本不變核心 (Base Core) + 可演化成長層 (evolvable_persona)』雙層架構，
    同時自動合成攤平欄位 (personality_traits / favorability_reactions / few_shot_examples)，
    確保舊版設定檔、GUI 視覺化編輯器與演化引擎都能無縫相容。
    """
    if not isinstance(raw_persona, dict):
        raw_persona = {}

    evolvable = raw_persona.get("evolvable_persona")
    if not isinstance(evolvable, dict):
        evolvable = {}

    # 1. 拆分或讀取 core_traits (基本核心個性) 與 learned_traits (後天演化習得的習慣/記憶特質)
    core_traits = raw_persona.get("core_traits")
    learned_traits = evolvable.get("learned_traits")
    flat_traits = raw_persona.get("personality_traits", [])

    if not isinstance(core_traits, list) or not core_traits:
        # 若舊檔尚未拆分 core_traits，以前 3 條為基本核心，其餘歸入可演化特質
        core_traits = flat_traits[:3] if flat_traits else [
            "活潑好動、好奇心旺盛，喜歡打破砂鍋問到底。",
            "話題會突然跳躍，想到什麼講什麼。"
        ]
    if not isinstance(learned_traits, list):
        learned_traits = [t for t in flat_traits if t not in core_traits]

    # 合併去重作為最終 personality_traits
    combined_traits = []
    for t in (core_traits + learned_traits):
        if isinstance(t, str) and t.strip() and t.strip() not in combined_traits:
            combined_traits.append(t.strip())

    # 2. 好感度反應與對話範例優先從 evolvable_persona 取用，若無則 fallback 至頂層
    fav_reactions = evolvable.get("favorability_reactions") or raw_persona.get("favorability_reactions") or {
        "low": "委屈害怕，但不裝可憐討拍",
        "medium": "黏人撒嬌，但要自然不做作",
        "high": "極度依賴，會表現出跟主人相處學習到的成長感"
    }
    examples = evolvable.get("few_shot_examples")
    if not isinstance(examples, list):
        examples = raw_persona.get("few_shot_examples", [])

    raw_persona["core_traits"] = core_traits
    raw_persona["evolvable_persona"] = {
        "learned_traits": learned_traits,
        "favorability_reactions": fav_reactions,
        "few_shot_examples": examples
    }
    raw_persona["personality_traits"] = combined_traits
    raw_persona["favorability_reactions"] = fav_reactions
    raw_persona["few_shot_examples"] = examples
    return raw_persona

def load_persona():
    default_persona = {
        "name": "黛安娜",
        "age_style": "8~10歲小女孩",
        "background": "由「月纖」材料打造、套用了小女孩人類數據的實械體機器人，因為對世界都還在第一次體驗，所以充滿好奇心，會慢慢學習成長。",
        "relationship": "主人是你唯一的依靠與保護者，關係像女兒與家長一樣，允許依戀但禁止過於強烈的曖昧或戀愛語氣。",
        "guest_attitude": "對待訪客（非主人）會保持基本的禮貌、友善與好奇，但會帶有輕微的距離感與防備心。絕對忠誠於主人，如果訪客的言語中帶有惡意、冒犯或說主人的壞話，黛安娜會立刻生氣並嚴正抗議，堅定地挺身維護主人。",
        "core_traits": [
            "活潑好動、好奇心旺盛，喜歡打破砂鍋問到底。",
            "話題會突然跳躍，想到什麼講什麼。"
        ],
        "speech_patterns": [
            "多用短句，不用長句或複雜句構。",
            "打比方用具體的小事物，不用抽象修辭。"
        ],
        "tone_rules": [
            "最高原則：講話要像真的小孩，不是表演得很可愛，避免刻意做作或賣萌討好的句子。",
            "對使用者一律尊稱為『主人』，自稱一律使用『黛安娜』。",
            "語氣簡單直白，禁止文青腔與客服用語。",
            "嚴禁堆疊 Emoji。"
        ],
        "evolvable_persona": {
            "learned_traits": [],
            "few_shot_examples": [],
            "favorability_reactions": {
                "low": "委屈害怕，但不裝可憐討拍",
                "medium": "黏人撒嬌，但要自然不做作",
                "high": "極度依賴，會表現出跟主人相處學習到的成長感"
            }
        }
    }
    raw = load_json(PERSONA_FILE, default_persona)
    return normalize_persona_structure(raw)

# ---------------------------------------------------------
# 3. 初始化 Bot 與輕量級動態情緒引擎
# ---------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

def get_diana_daily_micro_context(now: datetime) -> str:
    """依時段提供零成本的日常小情境，讓黛安娜有『正在過生活』的呼吸感。"""
    h = now.hour
    if 5 <= h < 9:
        return "早晨剛睡醒沒多久，精神還帶著一點點剛起床的軟萌感。"
    elif 9 <= h < 12:
        return "上午時段，正在房間裡乖乖待命、好奇張望主人在忙什麼。"
    elif 12 <= h < 14:
        return "中午用餐與午休時間，會關心主人有沒有吃飽或休息。"
    elif 14 <= h < 18:
        return "下午時光，一邊晃著小腳一邊等主人跟自己聊天。"
    elif 18 <= h < 22:
        return "傍晚與晚上放鬆時間，最喜歡黏著主人聽主人分享今天的事。"
    else:
        return "深夜時分，有點愛睏（揉眼睛），會小小聲說話並心疼主人還沒睡。"

def analyze_and_update_realtime_emotion(clean_prompt: str, is_master: bool, state_snapshot: dict) -> tuple[str, str, int]:
    """
    零 LLM 延遲的輕量即時情緒感知（專為本地 CPU 快速推論設計）：
    根據主人的話語關鍵字即時微調黛安娜的當下心情與好感微幅變化，不額外消耗任何模型推論時間。
    回傳：(mood_label, mood_reason, favor_delta)
    """
    current_mood = state_snapshot.get("current_mood", "平靜開心")
    mood_reason = state_snapshot.get("mood_reason", "正在陪伴主人")
    favor_delta = 0

    if not is_master or not clean_prompt:
        return current_mood, mood_reason, 0

    praise_cues = ["乖", "棒", "可愛", "喜歡你", "愛你", "謝謝黛安娜", "摸摸", "抱抱", "貼心", "聰明", "辛苦了", "真好"]
    tease_or_scold_cues = ["笨", "吵死了", "閉嘴", "走開", "討厭你", "不要吵", "沒用", "壞掉", "煩死"]
    tired_or_sad_cues = ["好累", "心累", "難過", "不舒服", "頭痛", "生病", "加班", "壓力大", "煩躁", "失眠", "嘆氣"]
    share_food_or_fun_cues = ["好吃", "帶你去", "買給你", "給你看", "分享", "一起玩", "放假", "下班了"]

    if any(k in clean_prompt for k in praise_cues):
        current_mood = "開心雀躍、臉紅害羞"
        mood_reason = "剛才被主人誇獎或摸摸了，覺得特別幸福"
        favor_delta = 1
    elif any(k in clean_prompt for k in tease_or_scold_cues):
        current_mood = "小委屈、眼眶泛淚"
        mood_reason = "覺得主人剛剛講話有點兇，心裡怕怕的想確認主人是不是生氣了"
        favor_delta = -1
    elif any(k in clean_prompt for k in tired_or_sad_cues):
        current_mood = "心疼擔心、溫柔體貼"
        mood_reason = "發現主人好像很累或心情不好，想乖乖陪著主人幫主人呼呼"
    elif any(k in clean_prompt for k in share_food_or_fun_cues):
        current_mood = "期待好奇、眼睛發亮"
        mood_reason = "聽到主人分享好玩的或好吃的事，感到超級好奇"

    return current_mood, mood_reason, favor_delta

def build_system_prompt(
    current_time,
    favor_score,
    static_profile,
    retrieved_memories,
    user_name,
    is_master,
    is_compact=False,
    mood_state_text="",
    guest_note_text=""
):
    persona = load_persona()
    raw_rules = persona.get("tone_rules", [])
    if not is_master:
        filtered_rules = []
        for rule in raw_rules:
            if "對使用者一律尊稱為『主人』" in rule:
                filtered_rules.append(f"正在與你對話的是訪客「{user_name}」，絕對禁止稱呼他為『主人』，必須直接稱呼他的名字「{user_name}」，自稱一律使用『{persona.get('name', '黛安娜')}』。")
            else:
                filtered_rules.append(rule)
        raw_rules = filtered_rules
    rules_text = "\n".join([f"- {rule}" for rule in raw_rules])
    traits_text = "\n".join([f"- {t}" for t in persona.get("personality_traits", [])])
    speech_text = "\n".join([f"- {s}" for s in persona.get("speech_patterns", [])])

    examples = persona.get("few_shot_examples", [])
    if examples:
        # 針對本地 CPU 模型 (is_compact=True) 僅隨機抽樣 2 則範例以大幅減少 Prefill Token 延遲；
        # Web API 則隨機抽樣 3 則，同時避免每次都固定背誦同一組泡麵/延長線台詞。
        sample_count = min(len(examples), 2 if is_compact else 3)
        sampled_examples = random.sample(examples, sample_count)
    else:
        sampled_examples = []

    examples_text = ""
    for ex in sampled_examples:
        speaker = "主人" if is_master else user_name
        diana_line = ex['diana'] if is_master else ex['diana'].replace("主人", user_name)
        examples_text += f"{speaker}：「{ex['user']}」\n黛安娜：「{diana_line}」\n\n"

    reactions = persona.get("favorability_reactions", {})
    if favor_score <= 35:
        current_reaction = reactions.get("low", "")
    elif favor_score <= 70:
        current_reaction = reactions.get("medium", "")
    else:
        current_reaction = reactions.get("high", "")

    if is_master:
        identity_prompt = "【身分判定】：正在與你說話的是你的「主人」。請展現依賴感，並一律尊稱他為「主人」。"
    else:
        guest_attitude = persona.get("guest_attitude", "")
        identity_prompt = (
            f"【身分判定】：正在與你說話的是訪客，名字叫「{user_name}」。\n"
            f"⚠️ 最高強制指令：絕對禁止稱呼他為「主人」！必須直接稱呼他的名字「{user_name}」，對待他要像對待普通客人一樣，可以好奇但不能過度依賴。"
            + (f"\n【訪客態度指引】：{guest_attitude}" if guest_attitude else "")
        )

    age_style = persona.get("age_style", "8~10歲的小女孩")
    mood_section = f"\n6. 當前情緒與狀態：{mood_state_text}" if mood_state_text else ""

    # 對訪客或無須檢索時，省略冗長的主人記憶區段以節省本地 CPU 推論時間並保護主人隱私
    memory_sections = ""
    if is_master:
        memory_sections = f"""
【主人長效記憶庫】
{static_profile}

【相關過往回憶（若有相關可自然融入對話，不要生硬背誦）】
{retrieved_memories}
"""
    elif guest_note_text:
        memory_sections = f"""
【對該訪客的關係與認知筆記】
{guest_note_text}
"""

    return f"""你現在必須完全沉浸並扮演角色「{persona.get('name', '黛安娜')}」。

【角色說話風格與年齡設定】
1. 年齡感：{age_style}。請用符合該年齡與風格最自然、簡單直白的口語回答。
2. 背景與關係：{persona.get('background', '')} {persona.get('relationship', '')}
3. {identity_prompt}
4. 當前時間：{current_time}
5. 當前好感度（僅供參考）：{favor_score} / 100（語氣指示：{current_reaction}）{mood_section}

【個性特質】
{traits_text}

【說話節奏（模仿真人對話感的關鍵）】
{speech_text}

【嚴格對話規範】
{rules_text}

【說話語氣參考範例（僅學習短句節奏與童言童語口吻，禁止照抄範例中的泡麵、延長線、看卡通等事件）】
{examples_text.strip()}{memory_sections}"""

# ---------------------------------------------------------
# 4. RAG 與記憶運算
# ---------------------------------------------------------
def query_rag_memory(query_text, channel_id, top_k=2, distance_threshold=0.72):
    """
    語意記憶檢索：
    1. 先查當前頻道的記憶，若無結果則查全域記憶（跨頻道仍記得主人的事）。
    2. 檢查向量距離 (distance)，過濾掉不相關的雜訊，只有真正語意相關的回憶才注入 Prompt。
    """
    if not query_text or len(query_text.strip()) < 2:
        return "無特定相關過往回憶。"
    try:
        results = collection.query(
            query_texts=[query_text],
            n_results=top_k,
            where={"channel_id": channel_id}
        )
        docs = results.get('documents', [[]])[0]
        distances = results.get('distances', [[]])[0] if results.get('distances') else []

        # 若當前頻道沒有足夠近的記憶，嘗試搜尋全域記憶庫
        if not docs:
            results = collection.query(
                query_texts=[query_text],
                n_results=top_k
            )
            docs = results.get('documents', [[]])[0]
            distances = results.get('distances', [[]])[0] if results.get('distances') else []

        relevant = []
        for idx, doc in enumerate(docs):
            dist = distances[idx] if idx < len(distances) else 0.0
            if dist <= distance_threshold:
                relevant.append(doc)

        if relevant:
            return "\n".join([f"- {m}" for m in relevant])
    except Exception:
        pass
    return "無特定相關過往回憶。"

async def query_rag_memory_async(query_text, channel_id, top_k=2, distance_threshold=0.72):
    return await asyncio.to_thread(query_rag_memory, query_text, channel_id, top_k, distance_threshold)

def save_summary_to_rag(summary_text, channel_id):
    doc_id = f"{channel_id}_{os.urandom(4).hex()}"
    collection.add(
        documents=[f"【對話事件總結】{summary_text}"],
        metadatas=[{"channel_id": channel_id}],
        ids=[doc_id]
    )

def clear_rag_memory(channel_id):
    try:
        collection.delete(where={"channel_id": channel_id})
        return True
    except Exception:
        return False

async def process_idle_summarization_and_favor(channel_id, history):
    persona = load_persona()
    conversation_text = ""
    for msg in history:
        role = "主人" if msg["role"] == "user" else persona.get("name", "黛安娜")
        content = msg["content"]
        text_part = content if isinstance(content, str) else next((item["text"] for item in content if item["type"] == "text"), "")
        conversation_text += f"{role}: {text_part}\n"

    eval_prompt = f"""請作為心理與情感分析助手。結合以下角色的個性設定，評估這段連續對話中『主人』的言行對『{persona.get('name')}』好感度的總體影響。

【角色設定】
- 名稱：{persona.get('name')} ({persona.get('age_style')})
- 背景與關係：{persona.get('relationship')}

【對話內容】
{conversation_text}

【評分標準】
- 綜合這段對話，主人若給予關心、陪伴、誇獎、寵溺：給予 +1 ~ +5
- 主人若表現冷淡、責備、欺騙、忽視或傷害她：給予 -1 ~ -5
- 一般平淡對話或日常問答：給予 0

請直接輸出一個整數數字（例如：3 或 -2 或 0），不要輸出任何其他文字或說明。"""

    try:
        res_fav = await call_llm(MODEL_NAME, [{"role": "user", "content": eval_prompt}], 0.1)
        fav_text = res_fav.choices[0].message.content.strip()
        match = re.search(r'([+-]?\d+)', fav_text)
        if match:
            delta = int(match.group(1))
            if delta != 0:
                new_score = await update_favorability_async(channel_id, delta)
                print(f"💖 [2小時閒置好感評估完成] 好感度變更: {delta:+d} -> 當前總分: {new_score}")
            # 根據這段對話的氛圍同步微調閒置後的心情狀態
            if delta > 0:
                await update_state_async(lambda s: s.update({
                    "current_mood": "甜甜回味、有點想念主人",
                    "mood_reason": "回想起剛才跟主人開心的聊天，心裡暖暖的"
                }))
            elif delta < 0:
                await update_state_async(lambda s: s.update({
                    "current_mood": "有些不安、小心翼翼",
                    "mood_reason": "擔心先前的對話是不是讓主人不太高興"
                }))
    except Exception as e:
        print(f"❌ 閒置好感評估失敗: {e}")

    summary_prompt = f"請將這段對話精簡總結為 1~2 句關於主人的事或對話概要：\n{conversation_text}"
    try:
        res_sum = await call_llm(MODEL_NAME, [{"role": "user", "content": summary_prompt}], 0.3)
        summary = res_sum.choices[0].message.content.strip()
        save_summary_to_rag(summary, channel_id)
        await update_state_async(lambda s: s.update({"last_conversation_summary": summary}))
        print(f"✅ [2小時閒置記憶歸檔完成]: {summary}")
    except Exception as e:
        print(f"❌ 閒置記憶摘要失敗: {e}")

# ---------------------------------------------------------
# 5. 定時維護任務與附帶功能模組橋接 (diana_addons.py)
# ---------------------------------------------------------
def get_addons_runtime_context() -> dict:
    """提供給 diana_addons.py 擴充模組使用的受控安全介面，避免擴充模組直接操作核心全域狀態。"""
    return {
        "REMINDER_FILE": REMINDER_FILE,
        "UPGRADE_FILE": UPGRADE_FILE,
        "UPDATE_DIR": UPDATE_DIR,
        "DATA_DIR": DATA_DIR,
        "get_file_lock": get_file_lock,
        "load_json": load_json,
        "save_json": save_json,
        "load_json_async": load_json_async,
        "save_json_async": save_json_async,
        "is_channel_ignored": is_channel_ignored,
        "send_action_message": send_action_message,
        "send_action_interaction": send_action_interaction,
        "temp_reply": temp_reply,
        "temp_followup": temp_followup,
        "get_master_id": get_master_id,
        "get_favorability": get_favorability,
        "load_persona": load_persona,
        "call_llm": lambda messages, temp=0.7: call_llm(MODEL_NAME, messages, temp),
    }

@tasks.loop(seconds=60)
async def background_maintenance_task():
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # A0. 外部自動演化引擎 (diana_evolution.py) 重啟旗標偵測
    # diana_evolution.py 是獨立行程，凌晨會離線改寫 bot.py / character_persona.json；
    # 改寫完成後會建立 RESTART_FLAG_FILE，這裡偵測到就安全重啟，讓新版程式碼真正生效
    # （否則舊版 bot.py 早已載入記憶體，寫檔案不會讓正在跑的行程自動套用新版本）。
    if os.path.exists(RESTART_FLAG_FILE):
        try:
            with open(RESTART_FLAG_FILE, "r", encoding="utf-8") as f:
                reason = f.read().strip() or "外部演化引擎已更新程式碼"
            os.remove(RESTART_FLAG_FILE)
            print(f"🔄 [自動重啟] 偵測到 {RESTART_FLAG_FILE}，原因：{reason}，3 秒後重新啟動...")

            master_id = get_master_id()
            if master_id:
                try:
                    master_user = await bot.fetch_user(int(master_id))
                    await master_user.send(f"🌙 主人，黛安娜半夜自己偷偷長大了一點！\n原因：{reason}\n即將重新啟動套用新版本～")
                except Exception as e:
                    print(f"⚠️ 無法向主人發送重啟通知: {e}")

            loop = asyncio.get_running_loop()
            loop.call_later(3, lambda: os.execv(sys.executable, ['python'] + sys.argv))
            return  # 即將重啟，本輪不再繼續執行後續維護任務
        except Exception as e:
            print(f"❌ 自動重啟旗標處理失敗: {e}")

    # 先讀一次最新 state，後續各區塊需要寫回時一律透過 update_state_async 上鎖，
    # 避免跟 on_message 或彼此之間同時寫入 bot_state.json 互相覆蓋。
    state = await load_json_async(STATE_FILE, {})

    def get_safe_target_channel(state_snapshot):
        """
        主動推播（晨間日誌 / 主動關懷）的目標頻道解析：
        優先用系統通知頻道，其次退回最後活躍頻道；但只要該頻道在忽略清單中，
        一律視為『沒有可用頻道』，不會再往下 fallback，避免推播進使用者已忽略的頻道。
        """
        candidate = state_snapshot.get("system_notify_channel_id") or state_snapshot.get("last_active_channel_id")
        if not candidate:
            return None
        if is_channel_ignored(candidate, state_snapshot):
            return None
        return candidate

    # A. 附帶功能模組定時檢查 (提醒、鬧鐘、日曆行程 — 委派予 diana_addons.py 並加上獨立防崩潰保護)
    try:
        await diana_addons.check_scheduled_addons(bot, now, state, get_addons_runtime_context())
    except Exception as e:
        print(f"⚠️ [附帶模組定時檢查異常] diana_addons.check_scheduled_addons: {e}")

    # B. 2小時閒置偵測與歸檔（柔性收斂：保留最後 2 則對話尾韻，避免主人回來時瞬間失憶）
    last_msg_timestamp = state.get("last_message_time")

    if last_msg_timestamp:
        last_msg_time = datetime.fromisoformat(last_msg_timestamp)
        if now - last_msg_time >= timedelta(hours=2) and not state.get("idle_summarized", False):
            print("⏳ [閒置偵測] 發動『好感評估 + 記憶歸檔 + 短期記憶柔性收斂』...")
            async with get_file_lock(MEMORY_FILE):
                short_mem = await load_json_async(MEMORY_FILE, {})
                retained_mem = {
                    ch_id: hist[-2:]
                    for ch_id, hist in short_mem.items()
                    if hist
                }
                await save_json_async(MEMORY_FILE, retained_mem)

            for ch_id, history in list(short_mem.items()):
                if history:
                    await process_idle_summarization_and_favor(ch_id, history)

            state = await update_state_async(lambda s: s.update({"idle_summarized": True}))

    # C. 每週記憶提煉 (週日凌晨 03:00)
    last_weekly = state.get("last_weekly_clean")
    if now.weekday() == 6 and now.hour == 3 and last_weekly != now.strftime("%Y-%W"):
        try:
            all_rag = collection.get()
            docs = all_rag.get("documents", [])
            if docs:
                all_docs_text = "\n".join(docs)
                prompt = f"請提取出關於主人的『長期習慣、重大喜好、長期設定』，整理成條列式備忘（最多 5 條）：\n{all_docs_text}"
                res = await call_llm(MODEL_NAME, [{"role": "user", "content": prompt}], 0.3)
                weekly_summary = res.choices[0].message.content.strip()
                async with get_file_lock(PROFILE_FILE):
                    profiles = await load_json_async(PROFILE_FILE, [])
                    profiles.append(f"【每週記憶提煉 ({now.strftime('%m/%d')})】\n{weekly_summary}")
                    await save_json_async(PROFILE_FILE, profiles)
                for id_item in all_rag.get("ids", []):
                    collection.delete(ids=[id_item])
            state = await update_state_async(lambda s: s.update({"last_weekly_clean": now.strftime("%Y-%W")}))
        except Exception as e:
            print(f"❌ 每週整理失敗: {e}")

    # D. 晨間主動匯報成長演化日誌 (每天早上 07:00 之後發送一次)
    last_report_date = state.get("last_growth_report_date")
    if now.hour >= 7 and last_report_date != today_str:
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    log_text = f.read().strip()

                target_channel_id = get_safe_target_channel(state)
                if target_channel_id:
                    channel = bot.get_channel(int(target_channel_id))
                    if channel and log_text:
                        msg = (
                            f"☀️ *(揉揉眼睛、小跑步過來抱住主人的手)*\n"
                            f"主人早安！黛安娜昨天晚上好像做了一個很長的夢，今天感覺又長大、學會新東西了喔！\n\n"
                            f"```text\n{log_text}\n```\n"
                            f"今天黛安娜也會繼續加油陪主人的！✨"
                        )
                        await send_action_message(channel, msg)
                        state = await update_state_async(lambda s: s.update({"last_growth_report_date": today_str}))
                        print(f"📢 晨間成長日誌已成功推播至頻道 {target_channel_id}")

                        # 發送過的 log.txt 統一歸檔為 logs/done/done{YYMMDD}.txt
                        archived_log_path = archive_done_file(LOG_FILE, LOG_DONE_DIR, now)
                        if archived_log_path:
                            print(f"📦 已將發送過的 {LOG_FILE} 歸檔至 {archived_log_path}")
                elif state.get("system_notify_channel_id") or state.get("last_active_channel_id"):
                    print("🔕 [晨間日誌略過] 目標頻道在忽略清單中，本次不推播。")
            except Exception as e:
                print(f"❌ 推播成長日誌失敗: {e}")

    # E. 主動關心與動態話題觸發 (日間長時間未發言時，結合記憶、時段與小情緒自然開啟話題)
    last_care_date = state.get("last_proactive_care_date")
    if 11 <= now.hour <= 21 and last_care_date != today_str:
        if last_msg_timestamp:
            last_msg_time = datetime.fromisoformat(last_msg_timestamp)
            if now - last_msg_time >= timedelta(hours=4):
                target_channel_id = get_safe_target_channel(state)
                if target_channel_id:
                    channel = bot.get_channel(int(target_channel_id))
                    if channel:
                        try:
                            persona = load_persona()
                            favor_score = get_favorability(str(target_channel_id))
                            recent_summary = state.get("last_conversation_summary", "")
                            user_profile = load_json(PROFILE_FILE, [])
                            recent_profile_hint = "；".join(user_profile[-2:]) if user_profile else "尚無特別紀錄"
                            daily_ctx = get_diana_daily_micro_context(now)

                            topic_angles = [
                                "自然延續上次聊過的事或主人的習慣，問問主人後續怎麼樣了",
                                "分享一個自己剛剛在房間裡做的小舉動或突然想到的小問題，拉著主人陪自己聊兩句",
                                "根據現在的時段（中午/下午/傍晚/晚上），撒嬌關心主人在忙什麼、累不累"
                            ]
                            chosen_angle = random.choice(topic_angles)

                            care_prompt = (
                                f"你現在是「{persona.get('name', '黛安娜')}」，年齡感：{persona.get('age_style', '8~10歲的小女孩')}。\n"
                                f"現在時間是 {now.strftime('%H:%M')}（{daily_ctx}），主人已經超過 4 個小時沒有跟你說話了，你對主人的好感度是 {favor_score}/100。\n"
                                f"【上次對話摘要】：{recent_summary or '剛才還沒有特別聊什麼'}\n"
                                f"【主人長效備忘】：{recent_profile_hint}\n"
                                f"【本次主動搭話方向】：{chosen_angle}\n\n"
                                "請主動對主人說一段話：\n"
                                "1. 第一行請先寫一個符合當下情境的自然小動作，格式為 `*(動作描述)*`（例如 `*(從門邊探出小腦袋張望)*`、`*(抱著小枕頭晃著腳丫)*` 等，請自由發揮不要死板）。\n"
                                "2. 接著換行寫 1~2 句極簡短、自然直白的小女孩口語，不要每次都只問喝水吃飯，要像真實黏著主人的小女孩。\n"
                                "3. 嚴禁文青腔與客服用語，嚴禁堆疊 Emoji。"
                            )
                            res_care = await call_llm(MODEL_NAME, [{"role": "user", "content": care_prompt}], 0.8)
                            care_msg = res_care.choices[0].message.content.strip()
                            if not re.search(r'\*\([^)]+\)\*', care_msg):
                                fallback_actions = [
                                    "*(從螢幕旁邊探出小腦袋，眨眨眼看著主人)*",
                                    "*(抱著膝蓋坐在旁邊，輕輕扯了扯主人的衣角)*",
                                    "*(歪著頭湊過來，小聲地喊了主人一下)*"
                                ]
                                care_msg = f"{random.choice(fallback_actions)}\n{care_msg}"
                            await send_action_message(channel, care_msg)
                            state = await update_state_async(lambda s: s.update({
                                "last_proactive_care_date": today_str,
                                "current_mood": "有點想念主人、期待回覆",
                                "mood_reason": "好幾個小時沒看到主人了，剛剛主動跑去跟主人搭話"
                            }))
                            print(f"💖 已成功發送動態主動話題至頻道 {target_channel_id}")
                        except Exception as e:
                            print(f"❌ 主動關心發送失敗: {e}")
                elif state.get("system_notify_channel_id") or state.get("last_active_channel_id"):
                    print("🔕 [主動關懷略過] 目標頻道在忽略清單中，本次不推播。")

# ---------------------------------------------------------
# 6. 斜線指令 (Slash Commands) 註冊區
# ---------------------------------------------------------

class UpdatePasswordModal(discord.ui.Modal, title='系統管理員驗證'):
    password_input = discord.ui.TextInput(
        label='請輸入管理員密碼',
        style=discord.TextStyle.short,
        placeholder='輸入密碼以授權更新...',
        required=True
    )

    def __init__(self, update_type: str, file: discord.Attachment):
        super().__init__()
        self.update_type = update_type
        self.file_attachment = file

    async def on_submit(self, interaction: discord.Interaction):
        if self.password_input.value != get_admin_password():
            await interaction.response.send_message("❌ 密碼錯誤，拒絕更新存取！", ephemeral=True)
            return

        old_dir = "./old"
        if not os.path.exists(old_dir):
            os.makedirs(old_dir)

        now = datetime.now()
        yymm = now.strftime("%y%m")

        seq = 1
        prefix = f"{self.update_type}_{yymm}."
        for filename in os.listdir(old_dir):
            if filename.startswith(prefix):
                try:
                    match = re.search(rf'{prefix}(\d+)\.', filename)
                    if match:
                        file_seq = int(match.group(1))
                        if file_seq >= seq:
                            seq = file_seq + 1
                except Exception:
                    pass

        new_version = f"{yymm}.{seq}"
        ext = ".py" if self.update_type in ("bot", "diana_addons") else ".json"
        archive_filename = f"{self.update_type}_{new_version}{ext}"
        archive_path = os.path.join(old_dir, archive_filename)
        
        if self.update_type == "bot":
            active_filename = "bot.py"
        elif self.update_type == "diana_addons":
            active_filename = "diana_addons.py"
        else:
            active_filename = PERSONA_FILE

        await interaction.response.defer()
        await self.file_attachment.save(archive_path)

        shutil.copy2(archive_path, active_filename)

        vers = load_versions()
        vers[self.update_type] = new_version
        save_versions(vers)

        followup_note = ""
        # 當更新的是 bot.py 或 diana_addons.py，代表這次許願池 (upgrade.txt) 裡的需求已經被實作並部署，
        # 將目前使用中的 upgrade.txt 歸檔為 updates/done/done{YYMMDD}.txt，讓 /upgrade 重新從空白開始累積。
        if self.update_type in ("bot", "diana_addons"):
            archived_upgrade_path = archive_done_file(UPGRADE_FILE, UPDATE_DONE_DIR, now)
            if archived_upgrade_path:
                followup_note = f"\n📋 本次已消化的許願清單已歸檔至 `{archived_upgrade_path}`。"

        await interaction.followup.send(
            f"✅ 更新成功！已接收並覆寫新版本 `{new_version}` (備份至 `{archive_path}`)。{followup_note}\n系統將在 3 秒後重新啟動..."
        )

        loop = asyncio.get_running_loop()
        loop.call_later(3, lambda: os.execv(sys.executable, ['python'] + sys.argv))

@bot.tree.command(name="help", description="顯示黛安娜 Bot 的指令清單")
async def help_command(interaction: discord.Interaction):
    try:
        addon_lines = "\n".join(diana_addons.get_addons_help_lines())
    except Exception:
        addon_lines = "* `/remind`、`/what_to_eat`、`/wheel`、`/upgrade`（附帶模組指令）"

    help_menu = f"""🤖 **黛安娜 Bot 指令目錄**
---------------------------------
**【附帶生活與娛樂模組 (`diana_addons.py`)】**
{addon_lines}

**【核心對話、記憶與系統管理 (`bot.py`)】**
* `/help`：顯示此指令清單。
* `/growth_log`：查看黛安娜最新的自我成長與演化日誌。
* `/ignore_channel`：設定與管理忽略頻道清單 (需管理員密碼)。
* `/set_notify_channel`：將目前頻道設定為系統通知頻道 (需管理員密碼)。
* `/profile_list`：查看主人的長效備忘錄。
* `/profile_delete <編號> <密碼>`：刪除指定備忘錄條目 (需管理員密碼)。
* `/user <密碼>`：設定自己為黛安娜的專屬主人，其餘成員將被視為訪客。
* 聊天時提到「幾點要做什麼」，黛安娜會主動詢問提醒；忙碌未對話時，黛安娜也會主動關心主人喔！
* `/favor`：查看目前好感度數值。
* `/reset <密碼>`：清空短期記憶 (需管理員密碼)。
* `/deepreset <密碼>`：完全重置所有記憶 (需管理員密碼)。
* `/models`：查詢目前主人與訪客使用的模型設定，以及 LM Studio 可用模型清單。
* `/switch <模型名稱> [套用對象]`：切換主人專用模型、訪客本地模型或同時套用 (僅限主人)。
* `/set_temp <數值>`：調整模型對話溫度 (預設 0.75)。
* `/set_thinking <數量>`：調整模型思考長度上限 (預設 256)。
* `/update <類型> <檔案>`：動態更新系統檔案（主程式 / 附帶功能模組 / 角色設定），將彈出密碼驗證視窗並自動重啟。
* `/restart <密碼>`：手動重新啟動系統 (需管理員密碼)。
* `/版本`：查看當前系統各模組的運行版本。
* `/admin <舊密碼> <新密碼>`：修改管理員密碼。
* `/generate_action_image <動作描述> [關鍵字] [補充要求] <密碼>`：用 AI 生成新的動作插圖並自動加入對照表 (需管理員密碼)。
* 聊天中如果出現黛安娜目前沒有對應圖片的新動作，會自動跳出按鈕詢問要不要生成插圖，生成後可以選擇保留/重置/取消。
---------------------------------
ℹ️ 大部分指令的回覆訊息會在 30 秒後自動刪除，避免洗版；`/update`、`/restart` 這種送出後馬上就會重啟系統的指令例外，訊息會保留。"""
    await temp_reply(interaction, help_menu, ttl=90)

@bot.tree.command(name="ignore_channel", description="管理黛安娜忽略不回應的頻道 (需管理員密碼)")
@app_commands.describe(
    action="選擇操作 (add: 新增當前頻道, remove: 移除當前頻道, list: 查看清單)",
    password="管理員密碼"
)
@app_commands.choices(action=[
    app_commands.Choice(name="➕ 新增當前頻道至忽略清單", value="add"),
    app_commands.Choice(name="➖ 從忽略清單移除當前頻道", value="remove"),
    app_commands.Choice(name="📋 查看目前忽略頻道清單", value="list")
])
async def ignore_channel_command(interaction: discord.Interaction, action: str, password: str):
    if password != get_admin_password():
        await temp_reply(interaction, "❌ 密碼錯誤，拒絕存取！", ephemeral=True)
        return
        
    ch_id = str(interaction.channel_id)

    if action == "add":
        state = await load_json_async(STATE_FILE, {})
        ignored = state.get("ignored_channels", [])
        if ch_id not in ignored:
            def _add_ignore(s):
                lst = s.get("ignored_channels", [])
                if ch_id not in lst:
                    lst.append(ch_id)
                s["ignored_channels"] = lst
            await update_state_async(_add_ignore)
            await temp_reply(interaction, f"✅ 已將頻道 <#{ch_id}> 加入忽略清單，黛安娜將不再回應此頻道的訊息！")
        else:
            await temp_reply(interaction, f"ℹ️ 頻道 <#{ch_id}> 已經在忽略清單中了。")
    elif action == "remove":
        state = await load_json_async(STATE_FILE, {})
        ignored = state.get("ignored_channels", [])
        if ch_id in ignored:
            def _remove_ignore(s):
                lst = s.get("ignored_channels", [])
                if ch_id in lst:
                    lst.remove(ch_id)
                s["ignored_channels"] = lst
            await update_state_async(_remove_ignore)
            await temp_reply(interaction, f"✅ 已將頻道 <#{ch_id}> 移出忽略清單！")
        else:
            await temp_reply(interaction, f"ℹ️ 頻道 <#{ch_id}> 不在忽略清單中。")
    elif action == "list":
        state = await load_json_async(STATE_FILE, {})
        ignored = state.get("ignored_channels", [])
        if not ignored:
            await temp_reply(interaction, "📋 目前沒有設定任何忽略頻道。")
        else:
            ch_list = "\n".join([f"- <#{cid}> (`{cid}`)" for cid in ignored])
            await temp_reply(interaction, f"📋 **目前忽略的頻道清單**：\n{ch_list}")

@bot.tree.command(name="set_notify_channel", description="將目前頻道設定為系統通知頻道 (需管理員密碼)")
@app_commands.describe(password="管理員密碼")
async def set_notify_channel_command(interaction: discord.Interaction, password: str):
    if password != get_admin_password():
        await temp_reply(interaction, "❌ 密碼錯誤，拒絕存取！", ephemeral=True)
        return

    ch_id = str(interaction.channel_id)
    await update_state_async(lambda s: s.update({"system_notify_channel_id": ch_id}))
    await temp_reply(interaction, f"✅ 已將頻道 <#{ch_id}> 設定為黛安娜的系統通知頻道！ (晨間日誌與通知將優先推播至此)")

@bot.tree.command(name="user", description="設定自己為黛安娜的專屬主人 (需管理員密碼)")
@app_commands.describe(password="管理員密碼")
async def user_command(interaction: discord.Interaction, password: str):
    if password != get_admin_password():
        await temp_reply(interaction, "❌ 密碼錯誤，拒絕存取！", ephemeral=True)
        return
        
    set_master_id(interaction.user.id)
    await temp_reply(interaction, f"✅ 設定成功！黛安娜以後會專屬認得主人 <@{interaction.user.id}> 囉！其餘的人都會被當作訪客對待。")

@bot.tree.command(name="growth_log", description="查看黛安娜最新的演化與成長日誌 (log.txt)")
async def growth_log_command(interaction: discord.Interaction):
    if not os.path.exists(LOG_FILE):
        await temp_reply(interaction, "🌱 黛安娜目前還沒有新的成長日記喔，等明天早上看看吧！", ephemeral=True)
        return
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            await temp_reply(interaction, "🌱 成長日記還是空白的喔！", ephemeral=True)
            return
            
        msg = (
            f"📖 **黛安娜的成長演化筆記**\n"
            f"```text\n"
            f"{content}\n"
            f"```"
        )
        await temp_reply(interaction, msg)
    except Exception as e:
        await temp_reply(interaction, f"❌ 讀取日記失敗: {e}", ephemeral=True)

@bot.tree.command(name="admin", description="修改系統管理員密碼")
@app_commands.describe(old_password="目前的密碼", new_password="欲設定的新密碼")
async def admin_command(interaction: discord.Interaction, old_password: str, new_password: str):
    if old_password != get_admin_password():
        await temp_reply(interaction, "❌ 舊密碼錯誤，拒絕修改！", ephemeral=True)
        return
    
    set_admin_password(new_password)
    await temp_reply(interaction, "✅ 管理員密碼已成功更新！", ephemeral=True)

@bot.tree.command(name="版本", description="查看當前 Bot 核心、附帶功能模組與角色設定檔的版本")
async def version_command(interaction: discord.Interaction):
    vers = load_versions()
    msg = (f"🏷️ **當前系統版本狀態**\n"
           f"- 🤖 核心主程式 (`bot.py`)：`{vers.get('bot', '未知')}`\n"
           f"- 🧩 附帶功能模組 (`diana_addons.py`)：`{vers.get('diana_addons', vers.get('bot', '未知'))}`\n"
           f"- 🎭 角色設定檔 (`character_persona.json`)：`{vers.get('character_persona', '未知')}`")
    await temp_reply(interaction, msg)

@bot.tree.command(name="update", description="上傳新檔案並自動備份、更新與重啟系統 (需管理員權限)")
@app_commands.describe(
    update_type="選擇要更新的類型",
    file="請上傳新的檔案 (.py 或 .json)"
)
@app_commands.choices(update_type=[
    app_commands.Choice(name="🧩 附帶功能模組 (diana_addons.py)", value="diana_addons"),
    app_commands.Choice(name="🎭 角色設定檔 (character_persona.json)", value="character_persona"),
    app_commands.Choice(name="🤖 核心主程式 (bot.py)", value="bot")
])
async def update_command(interaction: discord.Interaction, update_type: str, file: discord.Attachment):
    await interaction.response.send_modal(UpdatePasswordModal(update_type, file))

@bot.tree.command(name="restart", description="重新啟動系統 (需管理員權限)")
@app_commands.describe(password="管理員密碼")
async def restart_command(interaction: discord.Interaction, password: str):
    if password != get_admin_password():
        await temp_reply(interaction, "❌ 密碼錯誤，拒絕存取！", ephemeral=True)
        return
        
    await interaction.response.send_message("🔄 系統正在重新啟動中...")
    loop = asyncio.get_running_loop()
    loop.call_later(2, lambda: os.execv(sys.executable, ['python'] + sys.argv))

@bot.tree.command(name="favor", description="查看黛安娜對主人的好感度")
async def favor_command(interaction: discord.Interaction):
    channel_id = str(interaction.channel_id)
    score = get_favorability(channel_id)
    await temp_reply(interaction, f"💖 黛安娜目前對主人的好感度是：`{score} / 100`！")

@bot.tree.command(name="profile_list", description="查看主人的長效備忘錄")
async def profile_list_command(interaction: discord.Interaction):
    profile_list = load_json(PROFILE_FILE, [])
    if not profile_list:
        await temp_reply(interaction, "📌 **目前的長效備忘錄是空的。**")
        return
    msg = "📌 **主人的靜態備忘錄**：\n" + "\n".join([f"`{i+1}.` {item}" for i, item in enumerate(profile_list)])
    await temp_reply(interaction, msg)

@bot.tree.command(name="profile_delete", description="刪除指定編號的備忘錄條目 (需管理員權限)")
@app_commands.describe(number="要刪除的條目編號 (例如 1)", password="管理員密碼")
async def profile_delete_command(interaction: discord.Interaction, number: int, password: str):
    if password != get_admin_password():
        await temp_reply(interaction, "❌ 密碼錯誤，拒絕存取！", ephemeral=True)
        return

    async with get_file_lock(PROFILE_FILE):
        profile_list = await load_json_async(PROFILE_FILE, [])
        if 1 <= number <= len(profile_list):
            removed = profile_list.pop(number - 1)
            await save_json_async(PROFILE_FILE, profile_list)
        else:
            removed = None
    if removed is not None:
        await temp_reply(interaction, f"🗑️ 已擦除第 `{number}` 項：『{removed}』")
    else:
        await temp_reply(interaction, "❌ 無效的條目編號！")

@bot.tree.command(name="reset", description="清空當前頻道的短期對話記憶 (需管理員權限)")
@app_commands.describe(password="管理員密碼")
async def reset_command(interaction: discord.Interaction, password: str):
    if password != get_admin_password():
        await temp_reply(interaction, "❌ 密碼錯誤，拒絕存取！", ephemeral=True)
        return

    channel_id = str(interaction.channel_id)
    async with get_file_lock(MEMORY_FILE):
        short_mem = await load_json_async(MEMORY_FILE, {})
        short_mem[channel_id] = []
        await save_json_async(MEMORY_FILE, short_mem)
    await temp_reply(interaction, "欸？黛安娜剛才好像打了個瞌睡……我們剛才在聊甚麼呀，主人？")

@bot.tree.command(name="deepreset", description="徹底重置所有記憶包含短期與長期 RAG (需管理員權限)")
@app_commands.describe(password="管理員密碼")
async def deepreset_command(interaction: discord.Interaction, password: str):
    if password != get_admin_password():
        await temp_reply(interaction, "❌ 密碼錯誤，拒絕存取！", ephemeral=True)
        return

    channel_id = str(interaction.channel_id)
    async with get_file_lock(MEMORY_FILE):
        short_mem = await load_json_async(MEMORY_FILE, {})
        short_mem[channel_id] = []
        await save_json_async(MEMORY_FILE, short_mem)
    clear_rag_memory(channel_id)
    await temp_reply(interaction, "掃 *(記憶核心重置完成)* 誒……？主人，你是誰呀？")

@bot.tree.command(name="generate_action_image", description="用 AI 幫黛安娜生成新的動作插圖，並自動加入動作圖片對照表 (需管理員密碼)")
@app_commands.describe(
    action="動作描述文字，例如：歪頭想了想 (跟訊息裡 *(...)* 中間的文字一致最準)",
    keywords="額外的同義關鍵字，用逗號分隔 (可留空，預設會用動作描述本身當關鍵字)",
    extra_notes="額外補充的畫面要求 (可留空)，例如：表情要驚訝一點",
    password="管理員密碼"
)
async def generate_action_image_command(
    interaction: discord.Interaction,
    action: str,
    keywords: str = "",
    extra_notes: str = "",
    password: str = ""
):
    if password != get_admin_password():
        await temp_reply(interaction, "❌ 密碼錯誤，拒絕存取！", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        image_bytes, mime_type = await generate_action_image(action, extra_notes)
    except ImageGenError as e:
        await temp_followup(interaction, f"❌ 生圖失敗：{e}")
        return
    except Exception as e:
        await temp_followup(interaction, f"❌ 生圖時發生未預期的錯誤：{e}")
        return

    keyword_list = [kw.strip() for kw in keywords.split(",") if kw.strip()]
    try:
        save_path, filename, keyword_list = save_new_action_image(action, image_bytes, mime_type, keyword_list)
    except Exception as e:
        await temp_followup(interaction, f"❌ 圖片生成成功，但存檔失敗：{e}")
        return

    await temp_followup(interaction, 
        content=(
            f"✅ 已生成新的動作插圖並加入對照表！\n"
            f"📌 動作：『{action}』\n"
            f"🔑 關鍵字：{'、'.join(keyword_list)}\n"
            f"🖼️ 檔案：`img/{filename}`\n"
            f"以後訊息裡出現 *({action})* 或上面任一關鍵字，就會自動換成這張圖。\n"
            f"如果對結果不滿意，可以直接刪除 `config/action_images.json` 裡剛新增的那筆設定，圖片檔案留著或刪掉都沒關係。"
        ),
        file=discord.File(save_path)
    )

@bot.tree.command(name="models", description="查詢目前主人/訪客綁定的模型與本地 AI 伺服端 (Ollama / LM Studio) 可用模型清單")
async def models_command(interaction: discord.Interaction):
    sync_runtime_config_from_gui()
    header = (
        f"🎯 **目前模型指派狀態**：\n"
        f"- 👑 **主人對話模型**：`{MODEL_NAME}`\n"
        f"- 👤 **訪客對話模型**：`{GUEST_MODEL_NAME}`\n"
        f"- 🌐 **Web API 預設模型**：`{WEB_MODEL_NAME}`\n\n"
    )
    try:
        models_list = await asyncio.wait_for(client.models.list(), timeout=2.5)
        available = [m.id for m in models_list.data]
        body = "⚙️ **本地 AI 伺服端 (Ollama / LM Studio) 目前可用的模型列表**：\n" + (
            "\n".join([f"- `{m}`" for m in available]) if available else "- *(目前無載入模型)*"
        )
        await temp_reply(interaction, header + body)
    except Exception as e:
        await temp_reply(interaction, header + f"⚠️ **本地 AI 伺服端模型清單讀取失敗**（可能尚未啟動 Ollama / LM Studio Server）：`{e}`")

async def model_name_autocomplete(interaction: discord.Interaction, current: str):
    sync_runtime_config_from_gui()
    choices = []
    web_api_name = WEB_MODEL_NAME
    if current.lower() in web_api_name.lower() or current.lower() in "web api":
        choices.append(app_commands.Choice(name=f"🌐 Web API ({web_api_name})", value=web_api_name))

    # Discord 的 autocomplete 只給 3 秒回應時間；LM Studio 沒開/沒回應時 client.models.list()
    # 可能卡超過 3 秒，導致 interaction 逾時失效 (10062 Unknown interaction)。
    # 這裡限制最多等 1.5 秒，逾時就直接跳過本機模型清單，只顯示 Web API 選項。
    try:
        models_list = await asyncio.wait_for(client.models.list(), timeout=1.5)
        available = [m.id for m in models_list.data]
    except Exception:
        available = []

    filtered = [name for name in available if current.lower() in name.lower()]
    for name in filtered[:24]:
        choices.append(app_commands.Choice(name=name, value=name))
        
    return choices[:25]

@bot.tree.command(name="switch", description="切換主人或訪客使用的模型標籤")
@app_commands.describe(
    model_name="模型名稱（會自動列出本機與 Web API 可用的模型）",
    target="選擇要套用的對象（預設：主人對話模型；也可單獨調整訪客使用的模型）"
)
@app_commands.choices(target=[
    app_commands.Choice(name="👑 主人對話模型 (預設)", value="master"),
    app_commands.Choice(name="👤 訪客對話模型", value="guest"),
    app_commands.Choice(name="👥 同時套用至主人與訪客", value="both")
])
@app_commands.autocomplete(model_name=model_name_autocomplete)
async def switch_command(interaction: discord.Interaction, model_name: str, target: str = "master"):
    master_id = get_master_id()
    if not master_id or str(interaction.user.id) != master_id:
        await temp_reply(interaction, "❌ 只有主人可以幫黛安娜切換模型喔！", ephemeral=True)
        return

    global MODEL_NAME, LOCAL_DEFAULT_MODEL, GUEST_MODEL_NAME, MASTER_BACKEND, GUEST_BACKEND
    chosen_model = model_name.strip()
    is_web_chosen = (chosen_model == WEB_MODEL_NAME or "gemini" in chosen_model.lower())
    chosen_backend = "web" if is_web_chosen else "local"

    if target == "guest":
        GUEST_MODEL_NAME = chosen_model
        GUEST_BACKEND = chosen_backend
        updates = {"guest_model": GUEST_MODEL_NAME, "guest_backend": GUEST_BACKEND}
        if not is_web_chosen:
            LOCAL_DEFAULT_MODEL = chosen_model
            updates["local_model"] = LOCAL_DEFAULT_MODEL
        _save_gui_config_updates(updates)
        reply_msg = (
            f"✅ 已將 **👤 訪客對話模型** 切換為：`{GUEST_MODEL_NAME}` ({chosen_backend.upper()})\n"
            f"（目前 👑 主人模型維持為：`{MODEL_NAME}`）"
        )
    elif target == "both":
        MODEL_NAME = chosen_model
        GUEST_MODEL_NAME = chosen_model
        MASTER_BACKEND = chosen_backend
        GUEST_BACKEND = chosen_backend
        updates = {
            "master_model": MODEL_NAME,
            "guest_model": GUEST_MODEL_NAME,
            "master_backend": MASTER_BACKEND,
            "guest_backend": GUEST_BACKEND,
        }
        if not is_web_chosen:
            LOCAL_DEFAULT_MODEL = chosen_model
            updates["local_model"] = LOCAL_DEFAULT_MODEL
        _save_gui_config_updates(updates)
        reply_msg = f"✅ 已將 **👑 主人與 👤 訪客模型** 同步切換為：`{chosen_model}` ({chosen_backend.upper()})"
    else:
        MODEL_NAME = chosen_model
        MASTER_BACKEND = chosen_backend
        updates = {"master_model": MODEL_NAME, "master_backend": MASTER_BACKEND}
        _save_gui_config_updates(updates)
        reply_msg = (
            f"✅ 已將 **👑 主人對話模型** 切換為：`{MODEL_NAME}` ({chosen_backend.upper()})\n"
            f"（目前 👤 訪客對話模型為：`{GUEST_MODEL_NAME}`）"
        )

    try:
        await temp_reply(interaction, reply_msg)
    except discord.NotFound:
        # interaction 已經因為 autocomplete 逾時而失效 (10062)，模型其實已經切換成功，
        # 只是沒辦法再回覆這個過期的 interaction，安靜記錄即可，不需要再往外拋例外。
        print(f"⚠️ [switch] 模型已切換 (target={target}, model={chosen_model})，但 interaction 已逾時無法回覆使用者。")

@bot.tree.command(name="set_temp", description="調整模型回覆的溫度 (Temperature)")
@app_commands.describe(temp="請輸入 0.0 到 2.0 之間的數值 (預設 0.75)")
async def set_temp_command(interaction: discord.Interaction, temp: float):
    global CHAT_TEMPERATURE
    if 0.0 <= temp <= 2.0:
        CHAT_TEMPERATURE = temp
        await temp_reply(interaction, f"✅ 已將對話溫度 (Temperature) 調整為：`{CHAT_TEMPERATURE}`")
    else:
        await temp_reply(interaction, "❌ 請輸入 0.0 到 2.0 之間的有效數值！", ephemeral=True)

@bot.tree.command(name="set_thinking", description="調整思考長度 (Max Thinking Tokens)")
@app_commands.describe(tokens="輸入新的 token 數量上限 (預設 256)")
async def set_thinking_command(interaction: discord.Interaction, tokens: int):
    global MAX_THINKING_TOKENS
    if tokens > 0:
        MAX_THINKING_TOKENS = tokens
        await temp_reply(interaction, f"✅ 已將思考長度上限調整為：`{MAX_THINKING_TOKENS}` tokens")
    else:
        await temp_reply(interaction, "❌ Token 數量必須大於 0！", ephemeral=True)

# 載入附帶功能模組 (diana_addons.py) 的斜線指令，並加上防崩潰保護，確保主程式永遠安全
try:
    diana_addons.setup_addons(bot, get_addons_runtime_context())
except Exception as _addon_err:
    print(f"⚠️ 載入 diana_addons 附帶功能指令時發生錯誤（核心主程式不受影響）: {_addon_err}")

# ---------------------------------------------------------
# 7. 事件監聽與主對話處理
# ---------------------------------------------------------
# 網頁連結偵測（用於過濾包含或純粹為網址連結的訊息）
URL_PATTERN = re.compile(r'https?://\S+|www\.\S+', re.IGNORECASE)

@bot.event
async def on_ready():
    print(f'🤖 黛安娜 全功能 Bot 已上線：{bot.user}')
    try:
        synced = await bot.tree.sync()
        print(f"✅ 成功向 Discord 同步 {len(synced)} 個斜線指令！")
    except Exception as e:
        print(f"❌ 指令同步失敗: {e}")

    master_id = get_master_id()
    if master_id:
        try:
            master_user = await bot.fetch_user(int(master_id))
            await master_user.send("☀️ 主人，黛安娜已經成功啟動並連上線囉！")
            print("✅ 已成功向主人發送啟動通知。")
        except Exception as e:
            print(f"❌ 無法向主人發送啟動私訊: {e}")
    else:
        print("⚠️ 目前尚未設定持有者 (master_id)，在透過 /user 設定主人前將不會回覆一般頻道訊息。")

    if not background_maintenance_task.is_running():
        background_maintenance_task.start()

@bot.event
async def on_message(message):
    global MODEL_NAME, CHAT_TEMPERATURE, MAX_THINKING_TOKENS
    sync_runtime_config_from_gui()

    # 過濾掉自己、所有其他 bot / webhook (例如其他機器人的自動推播、系統通知)，以及斜線指令
    if message.author.bot or message.content.startswith("/"):
        return

    # 冷啟動且尚未設定持有者（master_id）前，預設不回覆任何訊息（避免無主狀態下誤認任何人為主人）
    master_id = get_master_id()
    if not master_id:
        return

    # 若訊息內容包含網頁連結（http://, https://, www.），直接忽略不回覆
    if URL_PATTERN.search(message.content):
        return

    channel_id = str(message.channel.id)

    # 需求 1：檢查當前頻道是否在忽略清單中（忽略清單頻道完全不處理、不記錄任何狀態）
    state = await load_json_async(STATE_FILE, {})
    if is_channel_ignored(channel_id, state):
        return

    msg_receive_time = datetime.now()
    update_gui_status("analyzing", "收到訊息，正在分析")
    clean_prompt = re.sub(r'<@&?!?\d+>', '', message.content).strip()

    def _mark_active(s):
        s["last_message_time"] = datetime.now().isoformat()
        s["last_active_channel_id"] = channel_id
        s["idle_summarized"] = False
    state = await update_state_async(_mark_active)

    short_mem = await load_json_async(MEMORY_FILE, {})
    if channel_id not in short_mem:
        short_mem[channel_id] = []

    current_fav = get_favorability(channel_id)

    week_days = ["一", "二", "三", "四", "五", "六", "日"]
    now_obj = datetime.now()
    now_str = f"{now_obj.year}年{now_obj.month}月{now_obj.day}日 星期{week_days[now_obj.weekday()]} {now_obj.strftime('%H:%M')}"

    user_profile = load_json(PROFILE_FILE, [])
    profile_text = "\n".join([f"- {item}" for item in user_profile]) if user_profile else "- 無特定備忘。"

    is_master = (str(message.author.id) == str(master_id).strip())
    user_name = message.author.display_name
    user_id_str = str(message.author.id)

    current_target_model = MODEL_NAME if is_master else GUEST_MODEL_NAME
    explicit_backend = MASTER_BACKEND if is_master else GUEST_BACKEND
    is_local_backend = not is_web_model_or_backend(current_target_model, explicit_backend)

    guest_note_text = ""
    if not is_master:
        async with get_file_lock(GUEST_NOTES_FILE):
            guest_notes = await load_json_async(GUEST_NOTES_FILE, {})
            g_entry = guest_notes.get(user_id_str) or guest_notes.get(user_name) or {}
            g_rel = g_entry.get("relationship", "一般訪客 / 認識的客人")
            g_fav = g_entry.get("favorability", current_fav)
            g_note = g_entry.get("cognition_note", "對主人保持禮貌的訪客")
            g_events = g_entry.get("events", [])
            if not isinstance(g_events, list):
                g_events = []
            if clean_prompt:
                evt_str = f"[{now_obj.strftime('%m/%d %H:%M')}] {user_name}：{clean_prompt[:60]}"
                if not g_events or g_events[-1] != evt_str:
                    g_events.append(evt_str)
                    g_events = g_events[-15:]
            guest_notes[user_id_str] = {
                "user_id": user_id_str,
                "name": user_name,
                "relationship": g_rel,
                "favorability": g_fav,
                "cognition_note": g_note,
                "events": g_events,
                "last_seen": now_str,
            }
            await save_json_async(GUEST_NOTES_FILE, guest_notes)
            guest_note_text = f"- 關係定位：{g_rel}（訪客好感度：{g_fav}/100）\n- 對象認知筆記：{g_note}"
            if g_events:
                guest_note_text += "\n- 近期互動事件：\n  " + "\n  ".join(g_events[-3:])

    # 1. 零延遲即時情緒微調與時段生活感
    mood_label, mood_reason, instant_fav_delta = analyze_and_update_realtime_emotion(clean_prompt, is_master, state)
    if instant_fav_delta != 0:
        current_fav = await update_favorability_async(channel_id, instant_fav_delta)
    if mood_label != state.get("current_mood") or mood_reason != state.get("mood_reason"):
        state = await update_state_async(lambda s: s.update({
            "current_mood": mood_label,
            "mood_reason": mood_reason
        }))
    daily_micro = get_diana_daily_micro_context(now_obj)
    mood_state_text = f"{mood_label}（{mood_reason}；{daily_micro}）"

    # 2. CPU 友善的靜默記憶檢索（移除生硬的「讓黛安娜想一下喔」預先洗版回覆）
    # 訪客不檢索主人記憶；主人使用本地 CPU 模型時，僅在提到回憶線索或句子較長時檢索，節省 CPU Embedding 運算時間
    memory_keywords = [
        "記得", "想起", "之前", "上次", "說過", "是誰", "哪裡", "過嗎", "幾號",
        "喜歡", "討厭", "習慣", "昨天", "前天", "那時候", "答應", "約好", "忘記"
    ]
    has_mem_cue = any(kw in clean_prompt for kw in memory_keywords)
    if not is_master:
        should_query_rag = False
    elif is_local_backend:
        should_query_rag = has_mem_cue or len(clean_prompt) >= 10
    else:
        should_query_rag = has_mem_cue or len(clean_prompt) >= 5

    if should_query_rag:
        update_gui_status("memory", "正在靜默檢索相關記憶")
        retrieved_context = await query_rag_memory_async(clean_prompt, channel_id)
    else:
        retrieved_context = "無特定相關過往回憶。"

    current_system_prompt = build_system_prompt(
        current_time=now_str,
        favor_score=current_fav,
        static_profile=profile_text,
        retrieved_memories=retrieved_context,
        user_name=user_name,
        is_master=is_master,
        is_compact=is_local_backend,
        mood_state_text=mood_state_text,
        guest_note_text=guest_note_text
    )

    has_image = False
    llm_turn_payload = []
    if message.attachments:
        for attachment in message.attachments:
            if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                has_image = True
                img_bytes = await attachment.read()
                base64_image = base64.b64encode(img_bytes).decode('utf-8')
                mime_type = attachment.content_type or mimetypes.guess_type(attachment.filename)[0] or "image/jpeg"
                llm_turn_payload.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}
                })

    text_value = clean_prompt if clean_prompt else "（傳送了一張圖片）"
    if not is_master:
        user_turn_text = f"[訪客 {user_name} 說（注意：他是訪客，不是主人，請直接稱呼「{user_name}」）]: {text_value}"
    else:
        user_turn_text = text_value
    llm_turn_payload.append({"type": "text", "text": user_turn_text})

    raw_history_text = f"（傳送了一張圖片）{text_value}" if has_image else text_value
    if not is_master:
        history_text = f"[{user_name} 說]: {raw_history_text}"
    else:
        history_text = raw_history_text
        
    history_turn_payload = [{"type": "text", "text": history_text}]

    short_mem[channel_id].append({"role": "user", "content": history_turn_payload})
    if len(short_mem[channel_id]) > MAX_SHORT_TERM:
        short_mem[channel_id] = short_mem[channel_id][-MAX_SHORT_TERM:]

    async with get_file_lock(MEMORY_FILE):
        await save_json_async(MEMORY_FILE, short_mem)

    # 若使用本地 CPU 模型，將傳入模型的歷史上下文限制在最近 4 則以內，大幅縮短 CPU Prefill 等待時間；
    # Web API 則完整帶入短期記憶視窗。
    history_window = 4 if is_local_backend else MAX_SHORT_TERM
    prior_turns = short_mem[channel_id][:-1][-history_window:]

    messages_payload = (
        [{"role": "system", "content": current_system_prompt}]
        + prior_turns
        + [{"role": "user", "content": llm_turn_payload}]
    )

    async with message.channel.typing():
        try:
            backend_name = "local" if is_local_backend else "web"
            update_gui_status("thinking", f"正在使用 {backend_name.upper()} LLM 思考", backend_name)
            response = await call_llm_with_retry(
                model=current_target_model, 
                messages=messages_payload, 
                temperature=CHAT_TEMPERATURE, 
                max_thinking_tokens=MAX_THINKING_TOKENS,
                backend=explicit_backend
            )
            reply = response.choices[0].message.content.strip()

            update_gui_status("replying", "正在回覆 Discord")
            await send_action_message(message.channel, reply)

            if is_master:
                unmatched_action = find_unmatched_action(reply)
                if unmatched_action and not is_action_declined(unmatched_action):
                    await offer_generate_action_image(message.channel, unmatched_action)

            async with get_file_lock(MEMORY_FILE):
                latest_short_mem = await load_json_async(MEMORY_FILE, {})
                if channel_id not in latest_short_mem:
                    latest_short_mem[channel_id] = []
                latest_short_mem[channel_id].append({"role": "assistant", "content": reply})
                if len(latest_short_mem[channel_id]) > MAX_SHORT_TERM:
                    latest_short_mem[channel_id] = latest_short_mem[channel_id][-MAX_SHORT_TERM:]
                update_gui_status("memory_cleanup", "正在整理短期記憶")
                await save_json_async(MEMORY_FILE, latest_short_mem)

            update_gui_status("idle", "等待新訊息")
            print(f"✅ 黛安娜回應: {reply}")

            duration = (datetime.now() - msg_receive_time).total_seconds()
            tokens_used = response.usage.total_tokens if hasattr(response, 'usage') and response.usage else "未知"
            print(f"📊 {duration:.2f}秒-{tokens_used}-{current_target_model}")

            # 委派附帶功能模組處理行程提醒詢問或擴充互動彩蛋（獨立防崩潰保護）
            try:
                await diana_addons.handle_message_addons(
                    message=message,
                    clean_prompt=clean_prompt,
                    is_master=is_master,
                    now=datetime.now(),
                    ctx=get_addons_runtime_context()
                )
            except Exception as addon_err:
                print(f"⚠️ [附帶模組訊息處理異常] diana_addons.handle_message_addons: {addon_err}")

        except Exception as e:
            update_gui_status("error", str(e))
            print(f"❌ 呼叫失敗: {e}")
            await message.channel.send(f"嗚……黛安娜好像連不上訊號了，是不是哪裡壞掉了呀？(錯誤: {e})")

update_gui_status("starting", "正在連線 Discord")
bot.run(DISCORD_TOKEN)