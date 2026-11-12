# Diana Bot — Persistent AI Companion & Control Center

Diana 是一個以 Python 建構的 **Persistent AI Companion / AI Agent 原型**，結合了持續運行的 Discord 互動核心（[`bot.py`](bot.py)）、安全隔離演化系統（[`diana_evolution.py`](diana_evolution.py) + [`diana_addons.py`](diana_addons.py)）、智慧自適應圖形化中控台（[`gui.py`](gui.py)）與零依賴原生冷安裝精靈（[`DianaSeedInstaller.exe`](DianaSeedInstaller.exe)）。

它並不是單純將 Discord 訊息轉送給 LLM 的聊天機器人。Diana 本身會持續運行並維護自己的記憶、角色設定、使用者關係與系統狀態；LLM 則作為需要理解、推理與生成內容時使用的智能模組。

> **核心理念：Model Intelligence ≠ System Intelligence**  
> **LLM 負責理解、推理與生成；Python 系統負責記住、執行、維持狀態與持續存在。**

---

## 🏗️ 基本運作方式與系統架構

Diana 的基本互動流程：

```text
Discord Message (忽略純網址與冷啟動未認主訊息)
      ↓
Python Runtime (bot.py + diana_addons.py)
      ↓
Analyze (身分判斷：主人 / 訪客、情緒與意圖)
      ↓
Memory Retrieval (短期記憶 + ChromaDB 長期向量記憶 + 訪客筆記)
      ↓
Persona + Context (核心人設 + 可演化個性層 + 系統狀態)
      ↓
LLM (主人 / 訪客獨立分流：Local API 或 Web API)
      ↓
Response + Action Image (文字回覆 + 對應動作插圖)
      ↓
Memory / State Update (對話紀錄、好感度、訪客事件筆記、排程)
      ↓
Discord
```

---

## ✨ 核心功能與設計特色

### 💬 AI 對話、冷啟動保護與雙引擎分流
- **冷啟動安全保護與純網址過濾**：在 `data/base.json` 尚未綁定主人（`master_id`）前，系統處於冷啟動保護狀態，預設不回覆任何陌生訊息，避免將訪客誤認為主人；首次啟動後由主人在 Discord 頻道輸入 `/user 0000`（預設管理員密碼為 `0000`）完成認主。訊息內容若僅為網頁連結也會自動忽略。
- **主人 / 訪客獨立推理引擎分流**：主人與訪客可分別指定使用 **Local API（Ollama / LM Studio）** 或 **Web API（如 Google Gemini）** 與各自獨立的模型名稱，支援透過 Discord 指令 `/switch`（可選 `master` / `guest` / `both`）或 GUI 儀表板即時熱切換，無須重啟 Bot。

### 🧠 長期記憶、短期記憶與訪客關係
- **Short-term Memory**：近期對話與上下文保存於 `data/memory.json`。
- **Long-term Memory**：使用 ChromaDB（`diana_memory_db/`）保存長期向量記憶，並在需要時自動檢索相關過去記憶注入上下文。
- **主人與訪客關係狀態**：維護主人長效備忘錄（`data/user_profile.json`）、好感度（`data/favorability.json`）、排程提醒（`data/reminders.json`），以及獨立的**訪客認知筆記與事件記憶（`data/guest_notes.json`）**。

### 💤 主動行為與模組化附帶功能（`diana_addons.py`）
- 系統會在背景持續運行，依狀態與無聊值主動發起話題、執行週期性記憶整理與提醒檢查。
- 附帶功能（提醒、日曆行程清單、自然語言時間偵測、美食與自訂幸運轉盤、演化許願池等）獨立封裝於 `diana_addons.py`，由 `bot.py` 透過 `try-except` 沙箱呼叫（`setup_addons`、`check_scheduled_addons`、`handle_message_addons`）。

### 🧬 安全演化隔離架構（`diana_evolution.py`）
- Evolution 與主要 Discord Bot 分開運行，擁有獨立的 API 金鑰、模型、日誌、備份與重啟機制。
- **雙重隔離保護**：
  1. **程式碼隔離**：`diana_evolution.py` 的程式碼演化僅能修改附帶模組 `diana_addons.py`，絕不覆寫核心主程式 `bot.py`。
  2. **人格雙層隔離**：`config/character_persona.json` 拆分為受保護的**基本核心**（`core_traits`、`speech_patterns`、`tone_rules`、`guest_attitude`、`background`、`relationship`）與**可演化動態個性層**（`evolvable_persona`：`learned_traits`、`favorability_reactions`、`few_shot_examples`），防止演化覆寫角色根本人設。

### 🖼️ 動作插圖與參考圖系統（`image_gen.py`）
- 具備獨立的 Image API 與 `img/reference/` 角色參考圖庫，可依對話動作關鍵字（`config/action_images.json`）搭配對應動作插圖，或在缺少動作圖片時透過 Image API 生成新圖片。

### 🌐 四組獨立憑證與 API 設定
系統嚴格隔離四組憑證檔案，互不干擾：
- `config/discord_token.txt`（Discord Bot Token）
- `config/google_api_key_chat.txt`（聊天專用 Web API Key）
- `config/google_api_key_evolution.txt`（演化專用 Key，支援第 2 行指定演化專用模型名稱）
- `config/google_api_key_image.txt`（動作插圖生成專用 Key）

---

## 🖥️ Diana Control Center — 智慧自適應中控台（`gui.py`）

Windows 環境可直接雙擊 `start_gui.bat`，或透過指令啟動：

```bash
python gui.py
```

### 架構與自適應介面設計
- **分層架構**：由 `RuntimeLifecycleState` / `ViewportLayoutState` 追蹤任務狀態並於 `finally` 重置，`ConfigRepository` 以原子寫入管理設定與記憶包，`ExecutionWorkers` 負責非同步背景作業。
- **螢幕感知與動態重排**：啟動自動計算約 84% 寬 × 82% 高並置中（最小 `780x500`）；左側導覽列依視窗寬度按比例縮放（展開 `19%`、窄視窗 `< 980px` 自動收合為圖示模式 `6%`，亦可用 `◀ / ▶` 手動切換）；儀表板 KPI 卡片於 `1x4` 與 `2x2` 間自動重排，下方日誌區採用可上下拖曳的 `tk.PanedWindow`。

### 中控台核心功能頁籤
1. **即時儀表板（Runtime Dashboard）**
   - **連線狀態與控制**：一鍵 `Start / Stop / Restart`，搭配 6 階段向量進度管線（`PipelineStepperCanvas`）與支援篩選、上色的即時終端機日誌。
   - **目前角色（點擊快速切換）**：點擊方塊即可掃描 `config/*.persona` 角色檔（JSON 格式），確認備份提示後直接載入覆寫 `config/character_persona.json`；亦支援匯出自訂命名的 `.persona` 角色檔。
   - **關係（訪客）與排程（提醒）雙獨立方塊**：
     - **👥 關係**：點擊快速查看與編輯**除主人以外的訪客**好感度、對象認知筆記與事件記憶。
     - **⏰ 排程**：點擊快速查看、新增或刪除 `data/reminders.json` 中的排程提醒。
   - **👑 主人引擎 / 👤 訪客引擎雙獨立方塊**：點擊可各自切換 **Local API**（自動偵測本地模型）或 **Web API**（內建關鍵字查詢框，可即時向 API `/models` 檢索可用模型並套用）。
2. **連線與獨立憑證設定**
   - 提供 Ollama（`11434`）與 LM Studio（`1234`）一鍵預設、四組獨立金鑰 `👁 顯示 / 🙈 隱藏` 切換與連線延遲測試。
3. **角色設定工坊（Persona Studio）**
   - 右上角提供 **Canvas 滑桿開關（`ModeSliderSwitch`）**，可於「📝 表單模式」與「`{ }` JSON 原始碼模式」間即時雙向同步切換。
   - 支援 **Diana Example**、**Blank Template**、**AI Generate（Local / Web API 生成三候選方案）** 及匯出 `.persona`。
4. **記憶管理中心（Memory Studio）**
   - 直接檢視與編輯 `data/user_profile.json`、`data/memory.json`、`data/favorability.json`、`data/base.json`、`data/reminders.json`、`data/guest_notes.json` 與 ChromaDB 記憶庫。
   - 支援一鍵匯出 / 匯入自定義 **`.memory` 記憶備份包**（底層為 ZIP 壓縮檔），預設匯出檔名為 `{當前角色名}-Backup-{yymmdd}-{流水號}.memory`。
5. **系統診斷、演化控制與補丁推送**
   - 視覺化檢查必要環境、套件、憑證、參考圖庫與 `.seed` 狀態；支援一鍵觸發 `diana_evolution.py --run-now`、封裝 `.seed` 種子包，以及透過頂部工具列 **「🩹 開啟補丁推送工具」** 啟動 `patch_pusher.py`（Patch Pusher v2.0）安裝並推送補丁。

---

## 🌱 零依賴原生分級冷安裝精靈（`DianaSeedInstaller.exe` + `.seed`）

專案內建基於 C#（`.NET Framework 4.x`，無須預裝 Python 或任何第三方執行環境）的分級引導式原生冷安裝與種子封裝精靈：

- **主要檔案**：
  - `DianaSeedInstaller.exe`：原生安裝與封裝執行檔（自由縮放視窗與滾輪容器，Windows 10/11 可直接雙擊執行）。
  - `Diana-Bot.seed`：無記憶純淨系統包（已自動剔除所有個人記憶、`data/*.json` 私密資料、`diana_memory_db/` 與 API Key / Token）。
  - `installer/DianaSeedInstaller.cs` & `installer/build_installer.bat`：完整 C# 原始碼與一鍵編譯腳本。
- **雙模式分級引導**：
  1. **⚡ 簡易完整安裝**：適合新手用戶，強制使用 `.seed` 無記憶包，自動部署至目標資料夾、安裝 Python 3.12 與依賴套件、安裝 Ollama 並建立桌面捷徑，完成後自動進入 **Discord Bot API Token 取得教學與一鍵儲存頁**（引導開啟三大 Intents 與輸入 `/user 0000` 認主）。
  2. **🛠️ 自訂安裝（推薦）**：逐頁設定系統來源（本地 `.seed` 或直接從 GitHub [Ponkotsunekotw/Diana-Bot](https://github.com/Ponkotsunekotw/Diana-Bot) 下載最新預覽版）、Python 環境、以及本地 AI 方案（Ollama / LM Studio / 純雲端 API）。
- **線上三級模型選單與保守硬體速度查表**：
  - 支援 **來源類型 → 研發廠牌（Google / Qwen / DeepSeek / GLM / NVIDIA）→ 版本與線上回報尺寸** 動態抓取。
  - 內建保守速度查表（假設中低端顯卡與 DDR4）：以 $\text{可用顯存} = \max(0,\ \text{VRAM} - 2.0\text{ GB})$ 與 $\text{可用 DRAM} = \text{實體記憶體} \times 60\%$ 計算模型填充率，自動顯示預估推論速度分級。
- **CLI 命令列用法**：
  ```bash
  # 啟動圖形化分級冷安裝精靈
  DianaSeedInstaller.exe

  # 將指定專案目錄封裝為無記憶 .seed 系統包
  DianaSeedInstaller.exe --pack-seed . Diana-Bot.seed

  # 將 .seed 純淨部署至目標資料夾
  DianaSeedInstaller.exe --extract-seed Diana-Bot.seed C:\Diana-Bot
  ```

---

## 📦 快速開始（手動部署）

1. **取得專案**：執行 `DianaSeedInstaller.exe` 進行冷安裝，或 Clone 本倉庫。
2. **啟動控制中心**：執行 `start_gui.bat`（或 `python gui.py`）。
3. **安裝依賴套件**：在 GUI 的診斷／設定頁面執行 `requirements.txt` 安裝。
4. **設定憑證與模型**：於「連線與金鑰」頁分別填入 Discord Token 與 Local / Web / Image API 設定。
5. **選擇或建立角色**：在儀表板點擊「目前角色」載入 `config/*.persona`，或至「角色設定工坊」設計角色。
6. **啟動並認主**：回到儀表板點擊 **Start**，待 Bot 上線後於 Discord 頻道輸入 `/user 0000` 完成主人綁定即可開始互動。

---

## ⚠️ Project Status & 🛣️ Future Direction

Diana 目前仍屬於 **Early Prototype / Experimental AI Agent**，重點在於實驗「以持續運行的軟體系統維護記憶、關係、排程與演化，並將 LLM 作為按需呼叫的推理模組」的架構原型，不建議直接用於高可靠度或安全關鍵用途。

未來預計研究方向包含：更完整的 World Model、Policy / Permission System、System 1 / System 2 分工、Vision 與 Audio / Voice 擴展、Web Tools、Smart Home、更多 Interface Adapter，以及從經驗中形成新的持久能力。
