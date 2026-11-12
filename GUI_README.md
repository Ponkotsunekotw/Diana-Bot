# Diana Bot Control Center — 智慧自適應中控台 (`gui.py`)

Windows 環境可直接執行 `start_gui.bat`，或透過指令啟動：

```bash
python gui.py
```

本中控台採用**模組化分層架構（Separation of Concerns）**、**事件廣播驅動（Signal / Broadcast Queue）**與**全視窗自適應排版設計**，支援各類螢幕解析度（720p / 1080p / 1440p / 4K）與 Windows High-DPI 縮放，無需變更 `bot.py`、`image_gen.py` 與 `diana_evolution.py` 核心流程即可完成設定、監控、角色設計、冷安裝種子封裝與補丁推送。

---

## 🏗️ 架構與設計原則

1. **狀態與廣播層（`RuntimeLifecycleState` / `ViewportLayoutState` / `BroadcastSignal`）**
   - 以明確的布林狀態旗標追蹤各項非同步任務（如 `is_bot_running_flag`、`is_testing_local_api_flag`、`is_testing_web_api_flag`、`is_generating_persona_flag`、`is_installing_deps_flag`、`is_running_evolution_flag`）。
   - 任務結束時於 `finally` 區塊重置旗標回初始待命狀態（IDLE / `False`），避免重複觸發與執行緒競爭。
2. **資料與設定存取層（`ConfigRepository`）**
   - 使用暫存檔原子替換（Atomic Write）讀寫 `config/gui_config.json` 與 `config/character_persona.json`。
   - 嚴格維持四組憑證檔案彼此獨立：
     - `config/discord_token.txt`
     - `config/google_api_key_chat.txt`（聊天專用）
     - `config/google_api_key_evolution.txt`（演化專用，支援檢視與編輯第 2 行的演化專用模型名稱，不會在儲存金鑰時被覆蓋）
     - `config/google_api_key_image.txt`（動作插圖生成專用）
3. **背景執行層（`ExecutionWorkers`）**
   - 獨立管理 `bot.py` 啟停、OpenAI / Gemini API 連線延遲探測、LLM 三候選角色生成、`pip install -r requirements.txt`、`diana_evolution.py --run-now` 手動演化觸發，以及 `DianaSeedInstaller.exe --pack-seed` 無記憶系統種子包封裝。
4. **現代化介面層（`DianaControlCenterApp`）**
   - **即時儀表板（Runtime Dashboard）**：提供 4 張即時系統指標卡（連線狀態、目前角色與版本、好感度/提醒/動作插圖統計、推理引擎參數）、向量繪製的 6 階段視覺化管線（`PipelineStepperCanvas`），以及支援關鍵字篩選與語法上色的終端機日誌。
   - **連線與獨立憑證設定**：分段式頁籤、Ollama (`11434 + gemma4:e2b`) 與 LM Studio (`1234 + google/gemma-4-e2b`) 一鍵預設按鈕、金鑰 `👁 顯示 / 🙈 隱藏` 切換、即時連線延遲與模型數量測試。
   - **角色設定工坊（Persona Studio）**：支援「📝 視覺化表單模式」與「`{ }` JSON 原始碼模式」雙向即時同步，將角色設定拆分為「🔒 基本核心設定（不可被 evo 修改）」與「🧬 可演化動態個性層（`evolvable_persona`）」，並可選擇使用 Local API 或 Web API 產生三個候選角色方案。
   - **記憶管理中心（Memory Studio）**：支援直接檢視與編輯主人長效備忘錄（`data/user_profile.json`）、各頻道短期對話記憶（`data/memory.json`）、好感度與主人綁定（`data/favorability.json`、`data/base.json`、`data/reminders.json`、`data/guest_notes.json`），以及 ChromaDB 長期向量記憶庫（`diana_memory_db/chroma.sqlite3`），並提供**一鍵匯出完整記憶包（`.memory`）**與**一鍵匯入還原記憶包（`.memory`）**功能。
   - **系統診斷與演化控制**：視覺化檢視 14 項必要環境、套件、憑證檔案、參考圖庫與 `.seed` 冷安裝代理狀態，並支援一鍵觸發單次離線演化或封裝無記憶 `.seed` 系統包。

---

## 📐 核心自適應視窗與排版特性

1. **螢幕感知初始視窗定位（`ViewportBreakpointController.compute_initial_geometry`）**
   - 啟動時自動偵測螢幕解析度與 DPI，動態計算最適合的初始寬高（約佔螢幕 84% 寬 × 82% 高，並自動置中），支援最小縮放至 `780x500` 而不會遮擋下方工作列。
2. **自適應可摺疊側邊導覽列（Proportional & Collapsible Navigation Rail）**
   - 側邊導覽列寬度依照視窗總寬度**按比例動態縮放**：展開模式下約佔視窗寬度 `19%`（範圍 `200px ~ 320px`），收合圖示模式下約佔視窗寬度 `6%`（範圍 `58px ~ 84px`）。
   - 當視窗寬度小於 `980px` 時，左側導覽列自動切換為收合圖示模式；當視窗拉寬時自動展開。
   - 亦可隨時點擊左上角品牌列右側的 `◀ / ▶` 按鈕手動切換收合或展開狀態。
3. **動態斷點網格重排（Dynamic Breakpoint Grid Reflow）**
   - **即時儀表板 KPI 卡片**：在寬螢幕／標準模式下採 `1x4` 四欄並排，當工作區變窄時自動切換為 `2x2` 雙欄網格，並即時更新文字自動換行寬度（`wraplength`），徹底消除文字截斷。
   - **上下可拖曳分割視窗（`tk.PanedWindow`）**：儀表板上方的「KPI 狀態 + 處理管線」與下方的「即時終端機日誌」之間提供可自由上下拉動的分隔線（Sash），方便隨時放大日誌視野。
   - **控制列自動換行**：當視窗寬度較窄時，「啟動／停止／重啟」按鈕群組與「日誌篩選／自動捲動／複製清除」工具列自動切換為上下雙排顯示，避免元件互相擠壓。
   - **向量管線進度圖（`PipelineStepperCanvas`）**：依據視窗實際寬度動態縮放 6 大階段節點間距、圓圈半徑與字體大小。
4. **全頁面自適應捲動容器（`ResponsiveScrollFrame`）**
   - 「連線與金鑰」、「角色設定工坊（視覺化表單）」與「系統診斷與演化」皆內建滑鼠滾輪自適應捲動容器，並在窄視窗下自動將雙欄表單重排為單欄垂直堆疊。
5. **內建一鍵啟動補丁推送小工具（`patch_pusher.py` — Patch Pusher v2.0）**
   - 頂部工具列右上角提供 **「🩹 開啟補丁推送工具」** 快速按鈕，可隨時呼叫新版 `Patch Pusher`（Mac 安裝程式風格拖曳介面），將 `.patch` / `.diff` 補丁拖進專案資料夾一鍵安裝、提交並透過 SSH 推送到 GitHub。

---

## 🧬 安全演化隔離架構（`diana_addons.py` + 雙層 `character_persona.json`）

- **主程式保護與附帶功能模組化**：
  - 將提醒、日曆行程清單、自然語言時間偵測、美食與自訂幸運轉盤、演化許願池等附帶功能獨立為 `diana_addons.py`。
  - `bot.py` 透過 `try-except` 沙箱呼叫 `diana_addons` 介面（`setup_addons`、`check_scheduled_addons`、`handle_message_addons`），而 `diana_evolution.py` 的程式碼演化（`evolve_code`）**僅會修改 `diana_addons.py`**，絕不觸碰核心 `bot.py`。
- **角色個性拆分（基本核心 vs. 可調整演化層）**：
  - `config/character_persona.json` 拆分為受保護的基本核心（`core_traits`、`speech_patterns`、`tone_rules`、`guest_attitude`、`background`、`relationship`）與可演化層（`evolvable_persona`：`learned_traits`、`favorability_reactions`、`few_shot_examples`），防止演化覆寫角色根本人設。

---

## 🌱 零依賴原生分級冷安裝精靈 v2.1（`DianaSeedInstaller.exe` + `.seed` 無記憶系統包）

專案內建基於 C#（透過 Windows 內建 `.NET Framework 4.x csc.exe` 編譯，無須預裝 Python 或任何第三方執行環境）的分級引導式原生冷安裝與種子封裝精靈：

- **主要檔案**：
  - `DianaSeedInstaller.exe`：零外部依賴原生安裝與封裝執行檔（僅約 74 KB，採用自由縮放自適應流式排版與滾輪容器，任何全新 Windows 10/11 電腦皆可直接雙擊執行）。
  - `Diana-Bot.seed`：預設產生的無記憶純淨系統包（標準 ZIP 相容結構，已自動剔除 `diana_memory_db/`、`data/memory.json`、`data/base.json`、`data/favorability.json`、`data/user_profile.json` 與所有私密 API Key / Discord Token，並將角色學習特徵重置為初始純淨狀態）。
  - `installer/DianaSeedInstaller.cs` & `installer/build_installer.bat`：完整 C# 原始碼與一鍵重編譯腳本。
- **傳統應用分級選單設計（Multi-Step Wizard）**：
  1. **第一頁：說明視窗右側獨立按鈕直接跳轉（無須勾選後按「下一步」）**：
     - **【1. 簡易完整安裝 ➔ 右側獨立按鈕「⚡ 進入簡易安裝 ➡」】**：適合完全不懂電腦的新手用戶，強制使用無記憶 `.seed` 系統包，無腦全選所有依賴項（自動部署 `.seed` 至 `C:\Diana-Bot`、自動安裝 Python 3.12 與依賴套件、自動安裝 Ollama 本地 AI 伺服端並建立桌面捷徑），安裝完成後自動進入 **Discord Bot API Token 取得與一鍵填寫教學頁**。
     - **【2. 自訂安裝（★ 推薦選項）➔ 右側獨立按鈕「🛠️ 進入自訂安裝 (推薦) ➡」】**：適合懂電腦的進階用戶，逐頁自行設定安裝細項：
       - **步驟 1 / 3（系統來源與目標路徑）**：可自由選擇 **「來源 A：使用本地 `.seed` 無記憶種子包」** 或 **「來源 B：🌐 預覽版（Preview）— 直接從 GitHub（[Ponkotsunekotw/Diana-Bot](https://github.com/Ponkotsunekotw/Diana-Bot)）下載安裝最新版」**，並自訂目標資料夾與桌面捷徑。
       - **步驟 2 / 3（Python 執行環境）**：自由選擇是否安裝 Python 3.12（或沿用既有環境）與是否自動安裝 `requirements.txt`。
       - **步驟 3 / 3（本地 AI 伺服端、前中後三級線上模型選單與保守速度查表）**：提供 **方案 A（安裝 Ollama + 拉取模型，Port 11434）**、**方案 B（使用 LM Studio 或其他相容端點，不裝 Ollama，僅指向 Port 1234）**、**方案 C（不安裝本地 AI，僅使用雲端 API）**。
  2. **線上動態模型列表抓取（前 / 中 / 後三級連動選單）與保守速度動態查表引擎**：
     - **前選單（來源類型）**：`🏛️ 官方模型庫 (Official Library)` vs. `🧪 客製與社群微調 (Custom / Community)`。
     - **中選單（研發廠牌）**：`Google (Gemma 4 / Gemma 3 系列)`、`Qwen (阿里通義千問 3.5 / 3 / 2.5)`、`DeepSeek (深度求索 R1 / V3)`、`GLM (智譜 ChatGLM 4)`、`NVIDIA (Nemotron 3 / Mini)`。
     - **後選單（版本與線上回報尺寸）**：自動從 Ollama 線上模型庫與 OCI Registry Manifest 抓取最新標籤與實際回報尺寸（如 `gemma4:e2b [4.6 GB]`、`qwen2.5:3b [1.9 GB]`、`gemma3:4b [3.4 GB]`、`deepseek-r1:7b [4.7 GB]` 等），亦支援手動輸入任意標籤並點擊「🔄 線上抓取最新列表與尺寸」即時查詢。
     - **內建保守硬體速度查表（假設使用中低端顯卡與 DDR4 記憶體）**：
       - **可用顯存計算**：$\text{可用顯存} = \max(0,\ \text{本地偵測顯存 VRAM} - 2.0\text{ GB})$（扣除 2GB 系統保留開銷）。
       - **可用 DRAM 計算**：$\text{可用 DRAM} = \text{系統實體記憶體 DRAM} \times 60\%$（容量打 6 折計算）。
       - 依據所選模型的回報尺寸（GB）計算可用顯存與可用 DRAM 的**相對填充率（%）**，自動對照內建 8 級保守速度查表（`Tier S 極速流暢 ~35-55 tokens/s` 至 `Tier F 記憶體不足 OOM`）並即時標示「👉 [目前落點]」。
  3. **安裝完成後的新手 Discord Bot API 引導頁**：
     - 內建「如何建立 Discord Application、開啟三大 Privileged Gateway Intents（含 `MESSAGE CONTENT INTENT`）、Reset Token 並透過 `/user 0000` 解除冷啟動保護綁定主人」的完整步驟說明。
     - 提供 **「🌐 一鍵開啟 Discord 開發者後台」**、**「🌐 開啟 Google AI Studio」** 按鈕，以及直接在安裝精靈內貼上 Token 儲存至 `config/discord_token.txt` 的快速設定框。
- **命令列（CLI）用法**：
  ```bash
  # 啟動圖形化分級冷安裝精靈
  DianaSeedInstaller.exe

  # 將指定專案目錄封裝為無記憶 .seed 系統包
  DianaSeedInstaller.exe --pack-seed . Diana-Bot.seed

  # 將 .seed 純淨部署至目標資料夾
  DianaSeedInstaller.exe --extract-seed Diana-Bot.seed C:\Diana-Bot
  ```
