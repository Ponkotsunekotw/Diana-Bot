# Diana Bot Control Center (Modernized Edition)

Windows 環境可直接執行 `start_gui.bat`，或透過指令啟動：

```bash
python gui.py
```

## 架構與設計原則

中控台（`gui.py`）採用**職責分離（Separation of Concerns）**與**事件廣播驅動（Signal / Broadcast Queue）**設計，在完全不更動 `bot.py`、`image_gen.py` 與 `diana_evolution.py` 核心業務流程的前提下，提供現代化的深色桌面管理介面：

1. **狀態與廣播層（`RuntimeLifecycleState` / `BroadcastSignal`）**：
   - 以明確的布林狀態旗標追蹤各項非同步任務（如 `is_bot_running_flag`、`is_testing_local_api_flag`、`is_generating_persona_flag`、`is_installing_deps_flag`、`is_running_evolution_flag`）。
   - 任務結束時於 `finally` 區塊重置旗標回初始待命狀態（IDLE / `False`），避免重複觸發與執行緒競爭。
2. **資料與設定存取層（`ConfigRepository`）**：
   - 使用暫存檔原子替換（Atomic Write）讀寫 `config/gui_config.json` 與 `config/character_persona.json`。
   - 嚴格維持四組憑證檔案彼此獨立：
     - `config/discord_token.txt`
     - `config/google_api_key_chat.txt`
     - `config/google_api_key_evolution.txt`（支援檢視與編輯第 2 行的演化專用模型名稱，不會在儲存金鑰時被覆蓋）
     - `config/google_api_key_image.txt`
3. **背景執行層（`ExecutionWorkers`）**：
   - 獨立管理 `bot.py` 啟停、OpenAI / Gemini API 連線延遲探測、LLM 三候選角色生成、`pip install -r requirements.txt` 與 `diana_evolution.py --run-now` 手動演化觸發。
4. **現代化介面層（`DianaControlCenterApp`）**：
   - **即時儀表板（Runtime Dashboard）**：提供 4 張即時系統指標卡（連線狀態、目前角色與版本、好感度/提醒/動作插圖統計、推理引擎參數）、向量繪製的 6 階段視覺化管線（`PipelineStepperCanvas`），以及支援關鍵字篩選與語法上色的終端機日誌。
   - **連線與獨立憑證設定**：分段式頁籤、金鑰 `👁 顯示 / 🙈 隱藏` 切換、即時連線延遲與模型數量測試。
   - **角色設定工坊（Persona Studio）**：支援「📝 視覺化表單模式」與「`{ }` JSON 原始碼模式」雙向即時同步，並可選擇使用 Local API 或 Web API 產生三個候選角色方案。
   - **系統診斷與演化控制**：視覺化檢視 8 項必要檔案與參考圖庫狀態，並支援一鍵觸發單次離線演化。
