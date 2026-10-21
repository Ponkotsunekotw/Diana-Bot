3. **背景執行層（`ExecutionWorkers`）**：
   - 獨立管理 `bot.py` 啟停、OpenAI / Gemini API 連線延遲探測、LLM 三候選角色生成、`pip install -r requirements.txt` 與 `diana_evolution.py --run-now` 手動演化觸發。
4. **現代化介面層（`DianaControlCenterApp`）**：
   - **即時儀表板（Runtime Dashboard）**：提供 4 張即時系統指標卡（連線狀態、目前角色與版本、好感度/提醒/動作插圖統計、推理引擎參數）、向量繪製的 6 階段視覺化管線（`PipelineStepperCanvas`），以及支援關鍵字篩選與語法上色的終端機日誌。
   - **連線與獨立憑證設定**：分段式頁籤、金鑰 `👁 顯示 / 🙈 隱藏` 切換、即時連線延遲與模型數量測試。
   - **角色設定工坊（Persona Studio）**：支援「📝 視覺化表單模式」與「`{ }` JSON 原始碼模式」雙向即時同步，並可選擇使用 Local API 或 Web API 產生三個候選角色方案。
   - **系統診斷與演化控制**：視覺化檢視 8 項必要檔案與參考圖庫狀態，並支援一鍵觸發單次離線演化。
=======
# Diana Bot Control Center — 智慧自適應中控台 (`gui.py`)

本中控台採用模組化分層架構（Separation of Concerns & State-Driven Broadcast Pattern）與全視窗自適應排版設計，支援各類螢幕解析度（720p / 1080p / 1440p / 4K）與 Windows High-DPI 縮放，無需變更 `bot.py` 核心流程即可完成設定、監控、角色設計與補丁推送。

## 核心自適應視窗與排版特性

1. **螢幕感知初始視窗定位 (`ViewportBreakpointController.compute_initial_geometry`)**
   - 啟動時自動偵測螢幕解析度與 DPI，動態計算最適合的初始寬高（約佔螢幕 84% 寬 × 82% 高，並自動置中），支援最小縮放至 `780x500` 而不會遮擋下方工作列。
2. **自適應可摺疊側邊導覽列 (Collapsible Navigation Rail)**
   - 當視窗寬度小於 `980px` 時，左側導覽列自動收合為 `68px` 圖示導覽列（釋放超過 `160px` 的水平工作區空間）；當視窗拉寬時自動展開為 `232px` 完整導覽列。
   - 亦可隨時點擊左上角品牌列右側的 `◀ / ▶` 按鈕手動切換收合或展開狀態。
3. **動態斷點網格重排 (Dynamic Breakpoint Grid Reflow)**
   - **即時儀表板 KPI 卡片**：在寬螢幕／標準模式下採 `1x4` 四欄並排，當工作區變窄時自動切換為 `2x2` 雙欄網格，並即時更新文字自動換行寬度（`wraplength`），徹底消除文字截斷。
   - **上下可拖曳分割視窗 (`tk.PanedWindow`)**：儀表板上方的「KPI 狀態 + 處理管線」與下方的「即時終端機日誌」之間提供可自由上下拉動的分隔線（Sash），方便隨時放大日誌視野。
   - **控制列自動換行**：當視窗寬度較窄時，「啟動／停止／重啟」按鈕群組與「日誌篩選／自動捲動／複製清除」工具列自動切換為上下雙排顯示，避免元件互相擠壓。
   - **向量管線進度圖 (`PipelineStepperCanvas`)**：依據視窗實際寬度動態縮放 6 大階段節點間距、圓圈半徑與字體大小。
4. **全頁面自適應捲動容器 (`ResponsiveScrollFrame`)**
   - 「連線與金鑰」、「角色設定工坊（視覺化表單）」與「系統診斷與演化」皆內建滑鼠滾輪自適應捲動容器，並在窄視窗下自動將雙欄表單重排為單欄垂直堆疊。
5. **內建一鍵啟動補丁推送小工具 (`patch_pusher.py`)**
   - 頂部工具列右上角提供 **「🩹 開啟補丁推送工具」** 快速按鈕，可隨時呼叫 `Diana Patch Pusher` 將 `.patch` 補丁安裝至本地並推送到 GitHub。
>>>>>>> theirs
