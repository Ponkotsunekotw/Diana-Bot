"""
diana_evolution.py
==================
黛安娜 (Diana) 自我成長與演化腳本

這支腳本是「離線自動演化引擎」，跟 bot.py 是兩個獨立行程：
- bot.py：常駐執行，負責 Discord 互動、回覆訊息、背景維護任務。
- diana_evolution.py：預設每天凌晨 04:00 執行一次（也可用 --run-now 手動觸發一次），
  離線改寫 character_persona.json / bot.py，讓黛安娜「長大」。

功能總覽：
- 透過 Google Gemini API 進行演化評估 (使用獨立於 bot.py 的專屬金鑰檔
  config/google_api_key_evolution.txt，跟聊天端的 config/google_api_key_chat.txt
  完全分開，可各自使用不同的 API 方案/專案/額度，也不會互相搶檔造成讀取錯誤)
- 初次啟動時向 Google 伺服器請求可用模型清單，由使用者手動選擇並記錄在金鑰檔第二行
- 讀取當前對話歷史、RAG 長期記憶庫、好感度狀態、舊版人設與程式碼
- 支援讀取 updates/upgrade.txt 執行主人主動要求的功能擴充，完成後歸檔為 done{YYMMDD}.txt
- 兩大安全演化模組（保護主程式與角色基本核心）：
  1. Persona Evolution (角色心智成長微調 -> 僅更新 config/character_persona.json 的 evolvable_persona 成長層，鎖定基本核心設定)
  2. Feature Evolution (附帶功能擴充 -> 僅更新 diana_addons.py，絕不直接修改 bot.py 核心主程式)
- 自動備份至 ./old 資料夾並依照 {type}_{YYMM}.{seq}.ext 版本號歸檔、更新 data/version.json
  (跟 bot.py 的 /update 指令共用同一套命名規則與備份資料夾，版本序號不會衝突)
- 自動生成「成長日誌 (logs/log.txt)」，供 Bot 隔天早上讀取並主動向主人匯報自己的成長
- 【與 bot.py 的整合】改寫完 bot.py 後，會建立 data/pending_restart.flag。
  bot.py 的背景任務每分鐘會檢查這個旗標，發現後會自動安全重啟並清除旗標，
  這樣新版程式碼才會真正被載入執行 (單純覆寫檔案不會讓「正在跑」的 process 自動更新)。

與 bot.py 保持一致的資料夾結構：
  config/   -> 密鑰、人設檔
  data/     -> 執行期狀態 json、版本紀錄、重啟旗標
  logs/     -> 成長日誌
  updates/  -> 功能許願池
  old/      -> 版本備份 (與 bot.py /update 指令共用，不要手動搬動裡面的檔案)
"""

import os
import sys
import json
import re
import time
import shutil
import schedule
from datetime import datetime
import chromadb
from google import genai
from google.genai import types

# ---------------------------------------------------------
# 1. 檔案路徑與基礎常數配置 (與 bot.py 的資料夾結構保持一致)
# ---------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_DIR = os.path.join(BASE_DIR, "config")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
UPDATE_DIR = os.path.join(BASE_DIR, "updates")
UPDATE_DONE_DIR = os.path.join(UPDATE_DIR, "done")
OLD_DIR = os.path.join(BASE_DIR, "old")
CHROMA_DIR = os.path.join(BASE_DIR, "diana_memory_db")

# 演化引擎專用金鑰檔，跟 bot.py 的聊天金鑰檔 (config/google_api_key_chat.txt) 完全分開。
# 這樣兩支程式互不讀寫對方的檔案：
#   - 你可以幫演化引擎跟聊天分別申請不同的 API 方案/專案/額度
#   - 不會因為其中一支程式改寫共用檔案格式，導致另一支程式讀取到錯誤內容而報錯
GOOGLE_API_KEY_FILE = os.path.join(CONFIG_DIR, "google_api_key_evolution.txt")
PERSONA_FILE = os.path.join(CONFIG_DIR, "character_persona.json")
BOT_FILE = os.path.join(BASE_DIR, "bot.py")
ADDONS_FILE = os.path.join(BASE_DIR, "diana_addons.py")
VERSION_FILE = os.path.join(DATA_DIR, "version.json")
MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")
PROFILE_FILE = os.path.join(DATA_DIR, "user_profile.json")
FAVOR_FILE = os.path.join(DATA_DIR, "favorability.json")
LOG_FILE = os.path.join(LOG_DIR, "log.txt")
UPGRADE_FILE = os.path.join(UPDATE_DIR, "upgrade.txt")
RESTART_FLAG_FILE = os.path.join(DATA_DIR, "pending_restart.flag")

def ensure_directories():
    for d in (CONFIG_DIR, DATA_DIR, LOG_DIR, UPDATE_DIR, UPDATE_DONE_DIR, OLD_DIR):
        os.makedirs(d, exist_ok=True)

ensure_directories()

# ---------------------------------------------------------
# 2. 工具函式：金鑰與模型設定、檔案讀寫、版本控制、日誌管理
# ---------------------------------------------------------
def get_api_config() -> tuple[str, str]:
    """讀取 google API key 與模型設定，若無模型則啟動互動選擇程序"""
    # 支援使用環境變數
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model_name = os.environ.get("GEMINI_MODEL_NAME", "").strip()

    key_path = GOOGLE_API_KEY_FILE
    if not os.path.exists(key_path):
        # 首次啟動遷移：如果偵測到舊版共用金鑰檔（曾經跟 bot.py 共用同一份），
        # 用「複製」而非「搬移」取得起始金鑰，避免影響 bot.py 聊天端仍在使用的檔案，
        # 之後兩邊就會是完全獨立、互不干涉的檔案。
        legacy_candidates = [
            os.path.join(CONFIG_DIR, "google API key -2.txt"),
            os.path.join(CONFIG_DIR, "google_api_key_chat.txt"),
            os.path.join(BASE_DIR, "google API key -2.txt"),
            os.path.join(BASE_DIR, "google API key.txt"),
            os.path.join(CONFIG_DIR, "google API key.txt"),
        ]
        for legacy_path in legacy_candidates:
            if os.path.exists(legacy_path):
                shutil.copy2(legacy_path, key_path)
                print(f"📦 [初始化] 從舊版共用金鑰檔複製了一份初始金鑰到 {key_path}")
                print(f"   （這只是複製，不會動到 {legacy_path}；接下來兩邊金鑰請自行決定是否要換成不同方案）")
                break

    if os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.read().splitlines() if line.strip()]

        if not api_key and lines:
            api_key = lines[0]
        if not model_name and len(lines) > 1:
            model_name = lines[1]

    if not api_key:
        print(f"❌ 找不到有效的 Google API Key！請建立 '{key_path}' 並在第一行貼入金鑰。")
        sys.exit(1)

    # 如果檔案中沒有記錄模型名稱，則向伺服器查詢並讓使用者選擇
    if not model_name:
        if not sys.stdin or not sys.stdin.isatty():
            model_name = "gemini-2.5-flash"
            print(f"⚠️ 非互動式執行環境且未設定演化模型，自動使用預設模型 '{model_name}'")
            with open(key_path, "w", encoding="utf-8") as f:
                f.write(f"{api_key}\n{model_name}\n")
            return api_key, model_name

        print("\n🔍 初次啟動或未設定模型，正在向 Google 伺服器查詢可用模型...")
        try:
            client = genai.Client(api_key=api_key)
            models = list(client.models.list())
            available_models = [m.name.replace("models/", "") for m in models if "gemini" in m.name]

            print("\n【請選擇您要用於演化的模型】")
            print("💡 建議選擇有免費額度的穩定模型，例如 gemini-2.5-flash 或 gemini-2.5-pro")
            for i, m_name in enumerate(available_models):
                print(f"{i+1}. {m_name}")

            while True:
                try:
                    choice = int(input("\n👉 請輸入模型編號 (數字): "))
                    if 1 <= choice <= len(available_models):
                        model_name = available_models[choice - 1]
                        break
                    else:
                        print("❌ 輸入超出範圍，請重試。")
                except ValueError:
                    print("❌ 請輸入有效的數字編號。")

            # 將 API Key 和選擇的模型寫回檔案
            with open(key_path, "w", encoding="utf-8") as f:
                f.write(f"{api_key}\n{model_name}\n")
            print(f"✅ 設定完成！模型 [{model_name}] 已成功儲存至 {key_path} 的第二行。\n")

        except Exception as e:
            print(f"❌ 查詢模型清單失敗: {e}")
            print("⚠️ 將自動使用預設模型 'gemini-2.5-flash'")
            model_name = "gemini-2.5-flash"
            with open(key_path, "w", encoding="utf-8") as f:
                f.write(f"{api_key}\n{model_name}\n")

    return api_key, model_name

def load_json(filepath, default_val):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ 讀取 {filepath} 失敗: {e}，返回預設值")
            return default_val
    return default_val

def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def generate_next_version(update_type: str) -> str:
    """
    計算符合 {type}_{YYMM}.{seq} 的下一版本號。
    跟 bot.py 的 /update 指令共用同一個 ./old 資料夾與命名規則，
    確保兩邊觸發的版本號不會互相覆蓋、序號可以正確接續。
    """
    os.makedirs(OLD_DIR, exist_ok=True)
    now = datetime.now()
    yymm = now.strftime("%y%m")
    seq = 1
    prefix = f"{update_type}_{yymm}."

    for filename in os.listdir(OLD_DIR):
        if filename.startswith(prefix):
            try:
                match = re.search(rf'{prefix}(\d+)\.', filename)
                if match:
                    file_seq = int(match.group(1))
                    if file_seq >= seq:
                        seq = file_seq + 1
            except Exception:
                pass
    return f"{yymm}.{seq}"

def archive_and_update(update_type: str, new_content: str) -> str:
    """將舊版封存到 ./old 並更新當前運行檔與 version.json，回傳新版本號"""
    new_version = generate_next_version(update_type)
    ext = ".py" if update_type in ("bot", "diana_addons") else ".json"
    archive_filename = f"{update_type}_{new_version}{ext}"
    archive_path = os.path.join(OLD_DIR, archive_filename)

    if update_type == "bot":
        active_filename = BOT_FILE
    elif update_type == "diana_addons":
        active_filename = ADDONS_FILE
    else:
        active_filename = PERSONA_FILE

    # 備份舊檔案
    if os.path.exists(active_filename):
        shutil.copy2(active_filename, archive_path)
        print(f"📦 已將上一版檔案備份至: {archive_path}")

    # 寫入新檔案
    with open(active_filename, "w", encoding="utf-8") as f:
        f.write(new_content)

    # 更新 version.json
    vers = load_json(VERSION_FILE, {"bot": "1.0", "diana_addons": "1.0", "character_persona": "1.0"})
    vers[update_type] = new_version
    save_json(VERSION_FILE, vers)
    print(f"🎉 成功更新 [{update_type}] 至版本 {new_version}！")
    return new_version

def archive_done_upgrade_request() -> str | None:
    """
    將已被消化的 updates/upgrade.txt 歸檔為 updates/done/done{YYMMDD}.txt，
    格式跟 bot.py 的 /update 流程統一，同一天重複歸檔會自動加 _2 / _3 尾碼。
    """
    if not os.path.exists(UPGRADE_FILE):
        return None
    with open(UPGRADE_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        return None

    date_str = datetime.now().strftime("%y%m%d")
    os.makedirs(UPDATE_DONE_DIR, exist_ok=True)
    dest_path = os.path.join(UPDATE_DONE_DIR, f"done{date_str}.txt")
    seq = 2
    while os.path.exists(dest_path):
        dest_path = os.path.join(UPDATE_DONE_DIR, f"done{date_str}_{seq}.txt")
        seq += 1

    shutil.move(UPGRADE_FILE, dest_path)
    return dest_path

def request_bot_restart(reason: str):
    """
    通知正在運行的 bot.py 行程：新版程式碼已就緒，請安全重啟。
    bot.py 的背景維護任務每 60 秒會檢查一次這個旗標檔案。
    """
    with open(RESTART_FLAG_FILE, "w", encoding="utf-8") as f:
        f.write(reason)
    print(f"🚩 已建立重啟旗標 ({RESTART_FLAG_FILE})，bot.py 下次背景任務循環時會自動安全重啟套用新版本。")

def write_growth_log(persona_summary: str, code_summary: str, persona_ver: str, code_ver: str):
    """
    寫入成長日誌到 logs/log.txt
    供 bot.py 於隔日早晨主動讀取並以黛安娜的語氣發送給主人
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    log_content = (
        f"【黛安娜成長演化日誌】\n"
        f"演化時間：{now_str}\n"
        f"角色設定版本：{persona_ver or '維持原樣'}\n"
        f"程式功能版本：{code_ver or '維持原樣'}\n"
        f"----------------------------------------\n"
        f"🌱 心智與回憶學習：\n{persona_summary}\n\n"
        f"🛠️ 技能與功能學習：\n{code_summary}\n"
    )
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(log_content)
    print(f"📝 成長日誌已寫入: {LOG_FILE}")

# ---------------------------------------------------------
# 3. 資料收集模組 (聚集記憶, 好感度, 近期對話)
# ---------------------------------------------------------
def collect_growth_context() -> dict:
    """彙整所有近期互動與成長記憶資料"""
    context = {}

    # 1. 好感度
    fav_data = load_json(FAVOR_FILE, {})
    avg_fav = sum(fav_data.values()) / max(len(fav_data), 1) if fav_data else 50
    context["favorability"] = fav_data
    context["avg_favorability"] = avg_fav

    # 2. 近期短期對話
    memory_data = load_json(MEMORY_FILE, {})
    all_turns = []
    for ch_id, history in memory_data.items():
        for turn in history:
            role = "主人" if turn.get("role") == "user" else "黛安娜"
            content = turn.get("content")
            if isinstance(content, list):
                text_part = next((item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"), "")
            else:
                text_part = str(content)
            all_turns.append(f"{role}: {text_part}")
    context["recent_dialogues"] = "\n".join(all_turns[-30:]) if all_turns else "近期無直接對話記錄。"

    # 3. 長期靜態備忘
    profiles = load_json(PROFILE_FILE, [])
    context["user_profiles"] = "\n".join([f"- {p}" for p in profiles]) if profiles else "無長效記憶標籤。"

    # 4. ChromaDB 向量回憶事件
    rag_docs = []
    if os.path.exists(CHROMA_DIR):
        try:
            chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
            try:
                collection = chroma_client.get_collection(name="diana_memories")
                data = collection.get()
                rag_docs = data.get("documents", []) or []
            except Exception:
                rag_docs = []
        except Exception as e:
            print(f"⚠️ 讀取 ChromaDB 失敗: {e}")
    context["rag_memories"] = "\n".join(f"- {d}" for d in rag_docs[-10:]) if rag_docs else "無近期歸檔的長期回憶事件。"

    return context

# ---------------------------------------------------------
# 4. 演化核心 1：角色人設心智微調 (Persona Evolution — 保護基本核心，僅演化成長層)
# ---------------------------------------------------------
def evolve_persona(client: "genai.Client", context: dict, model_name: str) -> tuple[str, str]:
    """
    評估並微調角色人設 (character_persona.json) 的【可調整成長層 (evolvable_persona)】。
    基本不變核心 (name, age_style, background, relationship, guest_attitude, core_traits, speech_patterns, tone_rules)
    由程式碼強制鎖定保護，不會被 LLM 意外覆寫或洗掉防護規則。
    回傳: (成長摘要, 新版本號)
    """
    print(f"\n🧠 [1/2] 正在使用 {model_name} 評估黛安娜的心智成長 (僅調整可演化成長區)...")
    current_persona = load_json(PERSONA_FILE, {})
    if not current_persona:
        print(f"❌ 找不到 {PERSONA_FILE} 或內容為空，略過人設演化。")
        return "找不到現有人設檔，略過本次心智成長。", ""

    # 分離【不變基本核心】與【可動態演化成長層】
    core_traits = current_persona.get("core_traits")
    if not isinstance(core_traits, list) or not core_traits:
        all_traits = current_persona.get("personality_traits", [])
        core_traits = all_traits[:3] if all_traits else [
            "活潑好動、天真爛漫、好奇心旺盛，喜歡打破砂鍋問到底。",
            "話題會突然跳躍，想到什麼講什麼，不用每次都緊扣主人的問題回答。",
            "遇到不懂的事情會表現出真的不知道的反應（歪頭、亂猜、亂問），不會裝懂或用大人的口吻分析事情。"
        ]

    evolvable_in = current_persona.get("evolvable_persona", {})
    if not isinstance(evolvable_in, dict):
        evolvable_in = {}

    current_learned_traits = evolvable_in.get("learned_traits")
    if not isinstance(current_learned_traits, list):
        all_traits = current_persona.get("personality_traits", [])
        current_learned_traits = [t for t in all_traits if t not in core_traits]

    current_fav_reactions = evolvable_in.get("favorability_reactions") or current_persona.get("favorability_reactions", {})
    current_examples = evolvable_in.get("few_shot_examples") or current_persona.get("few_shot_examples", [])

    immutable_base_summary = json.dumps({
        "name": current_persona.get("name", "黛安娜"),
        "age_style": current_persona.get("age_style", "8~10歲小女孩"),
        "background": current_persona.get("background", ""),
        "relationship": current_persona.get("relationship", ""),
        "guest_attitude": current_persona.get("guest_attitude", ""),
        "core_traits": core_traits,
        "speech_patterns": current_persona.get("speech_patterns", []),
        "tone_rules": current_persona.get("tone_rules", [])
    }, ensure_ascii=False, indent=2)

    evolvable_json_str = json.dumps({
        "learned_traits": current_learned_traits,
        "favorability_reactions": current_fav_reactions,
        "few_shot_examples": current_examples
    }, ensure_ascii=False, indent=2)

    prompt = f"""你是黛安娜 (Diana) 的心智成長設計師。
黛安娜的人設分為【不可變基本核心 (Base Core)】與【可動態演化成長區 (Evolvable Layer)】兩部分。
請參考以下不可變的基本核心，並根據近期與主人的互動記憶，僅對【可動態演化成長區】進行微幅成長調整。

【不可變基本核心 (僅供參考，系統已硬性鎖定，請勿違背)】
{immutable_base_summary}

【目前的可動態演化成長區 (請在此基礎上微調)】
{evolvable_json_str}

【近期與主人的互動與記憶數據】
- 當前平均好感度：{context['avg_favorability']} / 100
- 主人長期畫像與備忘：
{context['user_profiles']}
- 近期發生的回憶事件總結 (RAG)：
{context['rag_memories']}
- 近期對話片段：
{context['recent_dialogues']}

【成長微調指引】
1. `learned_traits`（後天習得的生活習慣與默契，建議維持 2~5 條）：
   - 記錄或微調黛安娜從主人身上新學會的事物、記住的主人習慣或相處小默契。
   - 請勿重複 `core_traits` 裡已有的天生個性。
2. `favorability_reactions`（好感度 `low` / `medium` / `high` 三階段語氣反應）：
   - 可依近期相處氛圍微幅潤飾，但必須保持小女孩的自然與童真。
3. `few_shot_examples`（真實說話範例，建議維持 4~6 則）：
   - 保留精華，汰換或新增 1~2 筆最能展現近期相處默契的小孩視角真實對話範例（格式為 `{{"user": "...", "diana": "..."}}`）。
4. 嚴禁將她改造成客套、文青或戀愛語氣。
5. 請回傳 JSON 格式，必須包含以下兩個欄位：
   - "growth_summary": 用 1~2 句繁體中文簡要說明這次黛安娜心智學會了什麼。
   - "evolvable_persona": 包含更新後的 `learned_traits`、`favorability_reactions`、`few_shot_examples` 的 JSON 物件。

請直接輸出合法 JSON，不要包含任何 markdown 說明標籤："""

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.4,
                response_mime_type="application/json"
            )
        )
        res_json = json.loads(response.text.strip())
        growth_summary = res_json.get("growth_summary", "今天黛安娜更了解主人的一點點習慣了！")
        new_evolvable = res_json.get("evolvable_persona", {})

        if not isinstance(new_evolvable, dict):
            raise ValueError("回傳的 evolvable_persona 格式不是有效的 JSON 物件")

        learned_traits = new_evolvable.get("learned_traits")
        fav_reactions = new_evolvable.get("favorability_reactions")
        few_shot_examples = new_evolvable.get("few_shot_examples")

        if not isinstance(learned_traits, list) or not isinstance(fav_reactions, dict) or not isinstance(few_shot_examples, list):
            raise ValueError("evolvable_persona 缺少 learned_traits、favorability_reactions 或 few_shot_examples")

        # 將演化後的成長層與原本鎖定的【基本核心】安全合併，保證核心規範 (如訪客規則、說話禁忌) 100% 不會被改壞
        merged_persona = dict(current_persona)
        merged_persona["core_traits"] = core_traits
        merged_persona["evolvable_persona"] = {
            "_comment": "【可動態演化成長區 (Evolvable Layer)】由 diana_evolution.py 每日根據互動記憶微調更新。",
            "learned_traits": learned_traits,
            "favorability_reactions": fav_reactions,
            "few_shot_examples": few_shot_examples,
        }
        # 同步合成頂層欄位以保持對所有模組與 GUI 的相容性
        combined_traits = []
        for t in (core_traits + learned_traits):
            if isinstance(t, str) and t.strip() and t.strip() not in combined_traits:
                combined_traits.append(t.strip())
        merged_persona["personality_traits"] = combined_traits
        merged_persona["favorability_reactions"] = fav_reactions
        merged_persona["few_shot_examples"] = few_shot_examples

        formatted_json = json.dumps(merged_persona, ensure_ascii=False, indent=2)
        new_ver = archive_and_update("character_persona", formatted_json)
        print("✅ 角色人設演化微調完成（基本核心已受保護鎖定，僅更新成長層）！")
        return growth_summary, new_ver
    except Exception as e:
        print(f"❌ 角色人設演化失敗: {e}")
        return f"角色心智演化遇到狀況，保持昨日狀態。(原因: {e})", ""

# ---------------------------------------------------------
# 5. 演化核心 2：附帶功能模組擴充評估 (Add-ons Evolution — 保護 bot.py 主程式)
# ---------------------------------------------------------
def evolve_code(client: "genai.Client", context: dict, model_name: str) -> tuple[str, str, bool]:
    """
    評估並擴充附帶功能模組 (`diana_addons.py`)，絕不直接修改 `bot.py` 核心主程式。
    有升級需求 (`updates/upgrade.txt`) 時優先處理，無需求時回傳 NO_CHANGE。
    回傳: (更新摘要, 新版本號, 是否有主動升級需求被處理)
    """
    print(f"\n⚙️ [2/2] 正在使用 {model_name} 評估附帶功能模組 (diana_addons.py) 擴充需求...")
    if not os.path.exists(ADDONS_FILE):
        print(f"❌ 找不到 {ADDONS_FILE}，略過程式碼評估。")
        return "找不到附帶功能模組 (diana_addons.py)，略過更新。", "", False

    with open(ADDONS_FILE, "r", encoding="utf-8") as f:
        current_code = f.read()

    # 讀取主動要求的功能 (updates/upgrade.txt)
    upgrade_request = ""
    if os.path.exists(UPGRADE_FILE):
        with open(UPGRADE_FILE, "r", encoding="utf-8") as f:
            upgrade_request = f.read().strip()
        if upgrade_request:
            print(f"📥 偵測到許願池內容！準備在 diana_addons.py 執行主人主動要求的功能升級...")

    prompt = f"""你是一位資深的 Python 與 Discord.py 軟體架構師。
為了保護核心主程式 (`bot.py`) 的絕對安全，黛安娜的所有「附帶生活工具與娛樂小功能」（如提醒、鬧鐘、日曆清單、餐點抽籤、幸運轉盤、小遊戲等）都獨立封裝在 `diana_addons.py` 模組中。
請根據近期主人與黛安娜的互動情境，評估是否需要為 `diana_addons.py` 新增實用功能或微調現有附帶指令。

【近期主人與黛安娜的對話與記憶概況】
- 長期備忘標籤：
{context.get('user_profiles', '無')}
- 近期對話片段：
{context.get('recent_dialogues', '無')}
- 當前好感度：{context.get('avg_favorability', 50)} / 100

【現有 diana_addons.py 程式碼】
{current_code}
"""

    if upgrade_request:
        prompt += f"""
【⚠️ 優先處理：主人主動要求的新增功能與修改方向】
{upgrade_request}
請務必在 `diana_addons.py` 中優先滿足並實作上述主人的明確要求。
"""

    prompt += """
【演化核心原則与模組介面契約（嚴格遵守）】
1. **必須完整保留以下 4 個對外入口函式與簽名（`bot.py` 會呼叫它們）**：
   - `setup_addons(bot, ctx: dict)`：在裡面使用 `@bot.tree.command(...)` 註冊斜線指令。
   - `check_scheduled_addons(bot, now: datetime, state: dict, ctx: dict)` (async)：每分鐘背景檢查提醒/鬧鐘/日曆。
   - `handle_message_addons(message, clean_prompt: str, is_master: bool, now: datetime, ctx: dict)` (async)：訊息回覆後的行程偵測或關鍵字彩蛋。
   - `get_addons_help_lines() -> list[str]`：回傳附帶模組的指令說明清單（供 `/help` 顯示）。
2. **善用 `ctx` 字典提供的安全工具**：
   - `ctx["temp_reply"](interaction, content, ephemeral=...)`：發送會自動清理的指令回覆。
   - `ctx["send_action_interaction"](interaction, text)` / `ctx["send_action_message"](channel, text)`：發送支援 `*(動作描述)*` 自動轉插圖的訊息。
   - `ctx["get_file_lock"](filepath)`、`ctx["load_json_async"](filepath, default)`、`ctx["save_json_async"](filepath, data)`、`ctx["DATA_DIR"]`。
3. **最小侵入性與穩定性**：
   - 保留現有的 `/upgrade`、`/what_to_eat`、`/wheel`、`/remind`、`/reminders` 以及自然語言行程提醒功能，不要刪除既有功能。
   - 如果沒有【主人主動要求】，且評估現有附帶功能已完全滿足需求、近期無新增功能的急迫性，請回傳 `NO_CHANGE`。

【輸出格式要求】
請回傳 JSON 格式：
- 若無需更新：
  {"status": "NO_CHANGE", "summary": "目前附帶功能模組運行穩定，無新增功能需求。"}
- 若決定新增或擴充功能：
  {"status": "UPDATED", "summary": "簡述本次在 diana_addons.py 新增了什麼技能或指令", "code": "完整且可執行的 diana_addons.py Python 程式碼"}"""

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                response_mime_type="application/json"
            )
        )
        res_json = json.loads(response.text.strip())
        status = res_json.get("status", "NO_CHANGE")
        summary = res_json.get("summary", "今日附帶功能模組保持原有穩定狀態。")

        if status == "NO_CHANGE":
            print("🛡️ [模組評估完成] 目前附帶功能運行穩定，今日無新增需求，保持現有 diana_addons.py。")
            return summary, "", False

        new_code = res_json.get("code", "").strip()
        if not new_code:
            print("⚠️ 未取得完整程式碼，保持原樣。")
            return "程式碼生成不完整，維持原狀。", "", False

        if new_code.startswith("```python"):
            new_code = new_code[9:]
        elif new_code.startswith("```"):
            new_code = new_code[3:]
        if new_code.endswith("```"):
            new_code = new_code[:-3]
        new_code = new_code.strip()

        # 1. 語法與編譯安全性檢驗 (Syntax Check)
        try:
            compile(new_code, "<diana_addons>", "exec")
        except SyntaxError as se:
            print(f"❌ 新增功能後的 diana_addons.py 未通過語法檢查 (SyntaxError): {se}，放棄本次更新以確保安全。")
            return f"新功能語法檢查未通過 ({se})，已安全取消更新。", "", False

        # 2. 介面契約檢查 (Contract Check)：確認必要入口函式皆存在於程式碼中
        required_hooks = ["def setup_addons", "async def check_scheduled_addons", "async def handle_message_addons"]
        missing_hooks = [h for h in required_hooks if h not in new_code]
        if missing_hooks:
            print(f"❌ 新增功能後的 diana_addons.py 缺少必要入口函式 {missing_hooks}，放棄本次更新以保護系統。")
            return f"新功能缺少必要模組介面 ({', '.join(missing_hooks)})，已安全攔截。", "", False

        new_ver = archive_and_update("diana_addons", new_code)

        # 成功升級後，將許願池 upgrade.txt 歸檔為統一格式 done{YYMMDD}.txt，避免重複執行
        had_upgrade_request = bool(upgrade_request)
        if had_upgrade_request:
            archived_path = archive_done_upgrade_request()
            if archived_path:
                print(f"🧹 主人主動要求已完成實作，許願清單已歸檔至: {archived_path}")

        print("🎉 黛安娜成功在 diana_addons.py 習得了新附帶功能並完成安全更新（bot.py 核心未受影響）！")
        return summary, new_ver, had_upgrade_request
    except Exception as e:
        print(f"❌ 附帶功能模組自主演化評估失敗: {e}")
        return f"程式演化評估過程異常: {e}，維持原狀。", "", False

# ---------------------------------------------------------
# 6. 主要排程與執行入口
# ---------------------------------------------------------
def run_evolution_cycle():
    print(f"\n=======================================================")
    print(f"🌟 [演化週期啟動] 時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"=======================================================")

    try:
        # 讀取金鑰與模型（初次執行會引導選擇）
        api_key, model_name = get_api_config()
        client = genai.Client(api_key=api_key)

        # 1. 搜集記憶與對話
        context = collect_growth_context()

        # 2. 演化角色人設 (主要心智成長)
        persona_summary, persona_ver = evolve_persona(client, context, model_name)

        # 加入緩衝時間，避免 Free Tier 的 TPM (每分鐘 Token 數) 被瞬間塞爆
        print("\n⏳ 為了避免觸發 Google API 頻率限制 (Rate Limit)，暫停 30 秒...")
        time.sleep(30)

        # 3. 演化與新增功能 (功能導向，無需求時保持現狀)
        code_summary, code_ver, upgrade_applied = evolve_code(client, context, model_name)

        # 4. 寫入成長日誌到 logs/log.txt 供 Bot 隔天早晨讀取
        write_growth_log(persona_summary, code_summary, persona_ver, code_ver)

        # 5. 若 diana_addons.py 附帶功能模組有實際更新，通知正在運行的 bot.py 行程安全重啟以套用新版本
        if code_ver:
            reason = "主人主動要求的附帶功能升級已實作完成" if upgrade_applied else "自主附帶功能演化已產生新版本"
            request_bot_restart(f"{reason} (diana_addons 版本 {code_ver})")

        # 驗證是否真的成功
        if "異常" in code_summary or "狀況" in persona_summary:
            print(f"\n⚠️ 今日演化循環已結束，但過程中發生了 API 錯誤 (詳見 logs/log.txt)，請檢查額度或網路狀態。\n")
        else:
            print(f"\n✨ 今日演化循環圓滿完成！日誌已儲存至 logs/log.txt，黛安娜又成長了一點點 🌱\n")

    except Exception as e:
        print(f"❌ 演化流程發生異常: {e}")

def main():
    print("🚀 黛安娜自我成長與演化引擎 (Diana Evolution Engine) 已啟動")
    print("📌 預設每日凌晨 04:00 自動執行一次演化微調與日誌產出。")

    # 確保啟動時就驗證金鑰與模型設定
    get_api_config()

    # 支援手動觸發參數
    if len(sys.argv) > 1 and sys.argv[1] in ["--run-now", "-now", "run"]:
        run_evolution_cycle()
        return

    # 設定每日定時排程 (每天凌晨 04:00 執行)
    schedule.every().day.at("04:00").do(run_evolution_cycle)

    print("\n⏳ 排程監聽中... (可按 Ctrl + C 結束)")
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
