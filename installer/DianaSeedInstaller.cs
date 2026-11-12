using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("Diana-Bot Cold-Install & Seed Wizard")]
[assembly: AssemblyDescription("Zero-dependency multi-step native installer and memory-free .seed packager for Diana-Bot")]
[assembly: AssemblyCompany("Diana-Bot Project")]
[assembly: AssemblyProduct("DianaSeedInstaller")]
[assembly: AssemblyVersion("2.0.0.0")]
[assembly: AssemblyFileVersion("2.0.0.0")]

namespace DianaSeedInstaller
{
    internal static class Program
    {
        [DllImport("user32.dll")]
        private static extern bool SetProcessDPIAware();

        [DllImport("kernel32.dll")]
        private static extern bool AttachConsole(int dwProcessId);

        private const int ATTACH_PARENT_PROCESS = -1;

        [STAThread]
        private static int Main(string[] args)
        {
            try
            {
                ServicePointManager.Expect100Continue = true;
                ServicePointManager.SecurityProtocol = (SecurityProtocolType)3072 | SecurityProtocolType.Tls11 | SecurityProtocolType.Tls;
            }
            catch
            {
            }

            // Headless CLI mode support
            if (args != null && args.Length > 0)
            {
                string firstArg = args[0].Trim().ToLowerInvariant();
                if (firstArg == "--pack-seed" || firstArg == "-p")
                {
                    AttachConsole(ATTACH_PARENT_PROCESS);
                    string srcDir = args.Length > 1 ? args[1] : AppDomain.CurrentDomain.BaseDirectory;
                    string outSeed = args.Length > 2 ? args[2] : Path.Combine(srcDir, "Diana-Bot.seed");
                    try
                    {
                        Console.WriteLine("=== [DianaSeedInstaller] 開始封裝無記憶 .seed 系統包 ===");
                        int count = SeedPackageEngine.CreateCleanSeedPackage(srcDir, outSeed, delegate(string msg)
                        {
                            Console.WriteLine(msg);
                        });
                        Console.WriteLine(string.Format("=== [成功] 共封裝 {0} 個檔案至: {1} ===", count, outSeed));
                        return 0;
                    }
                    catch (Exception ex)
                    {
                        Console.Error.WriteLine("[錯誤] 封裝 .seed 失敗: " + ex.Message);
                        return 1;
                    }
                }
                else if (firstArg == "--extract-seed" || firstArg == "-x")
                {
                    AttachConsole(ATTACH_PARENT_PROCESS);
                    string seedFile = args.Length > 1 ? args[1] : Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "Diana-Bot.seed");
                    string targetDir = args.Length > 2 ? args[2] : @"C:\Diana-Bot";
                    try
                    {
                        Console.WriteLine("=== [DianaSeedInstaller] 開始解壓縮並初始化無記憶 .seed 系統包 ===");
                        int count = SeedPackageEngine.ExtractSeedPackage(seedFile, targetDir, delegate(string msg)
                        {
                            Console.WriteLine(msg);
                        });
                        Console.WriteLine(string.Format("=== [成功] 共部署 {0} 個檔案至: {1} ===", count, targetDir));
                        return 0;
                    }
                    catch (Exception ex)
                    {
                        Console.Error.WriteLine("[錯誤] 部署 .seed 失敗: " + ex.Message);
                        return 1;
                    }
                }
                else if (firstArg == "--help" || firstArg == "-h" || firstArg == "/?")
                {
                    AttachConsole(ATTACH_PARENT_PROCESS);
                    Console.WriteLine("DianaSeedInstaller.exe - Diana-Bot 冷安裝與無記憶種子封裝精靈 v2.0");
                    Console.WriteLine("用法:");
                    Console.WriteLine("  DianaSeedInstaller.exe                                       啟動分級式圖形化冷安裝精靈 (GUI)");
                    Console.WriteLine("  DianaSeedInstaller.exe --pack-seed [來源目錄] [輸出.seed路徑]  將專案封裝為無記憶 .seed 檔案");
                    Console.WriteLine("  DianaSeedInstaller.exe --extract-seed [.seed路徑] [目標目錄]   將 .seed 純淨部署至目標資料夾");
                    return 0;
                }
            }

            try
            {
                SetProcessDPIAware();
            }
            catch
            {
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new InstallerWizardForm());
            return 0;
        }
    }

    /// <summary>
    /// Detects GPU Name, true 64-bit GPU VRAM (via Registry / nvidia-smi), and System Physical DRAM (via Win32 API),
    /// and evaluates inference speed using a built-in conservative lookup table (assuming mid/low-end GPU + DDR4)
    /// based on (VRAM - 2GB) and (System DRAM * 60%) relative fill percentages.
    /// </summary>
    internal sealed class HardwareProfile
    {
        public string GpuName { get; set; }
        public double VramGb { get; set; }
        public double SystemDramGb { get; set; }
        public string RecommendedModelTag { get; set; }
        public string OverallSummary { get; set; }

        public double UsableVramGb
        {
            get { return Math.Max(0.0, Math.Round(VramGb - 2.0, 1)); }
        }

        public double UsableDramGb
        {
            get { return Math.Round(SystemDramGb * 0.60, 1); }
        }

        public double TotalUsableMemoryGb
        {
            get { return Math.Round(UsableVramGb + UsableDramGb, 1); }
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Auto)]
        private class MEMORYSTATUSEX
        {
            public uint dwLength;
            public uint dwMemoryLoad;
            public ulong ullTotalPhys;
            public ulong ullAvailPhys;
            public ulong ullTotalPageFile;
            public ulong ullAvailPageFile;
            public ulong ullTotalVirtual;
            public ulong ullAvailVirtual;
            public ulong ullAvailExtendedVirtual;

            public MEMORYSTATUSEX()
            {
                this.dwLength = (uint)Marshal.SizeOf(typeof(MEMORYSTATUSEX));
            }
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GlobalMemoryStatusEx([In, Out] MEMORYSTATUSEX lpBuffer);

        public static HardwareProfile Detect()
        {
            HardwareProfile profile = new HardwareProfile();
            profile.GpuName = "整合式顯示晶片 / 未偵測到獨立顯卡";
            profile.VramGb = 0.0;
            profile.SystemDramGb = 8.0;
            profile.RecommendedModelTag = "gemma4:e2b";

            // 1. Detect System Physical DRAM via GlobalMemoryStatusEx
            try
            {
                MEMORYSTATUSEX memStatus = new MEMORYSTATUSEX();
                if (GlobalMemoryStatusEx(memStatus) && memStatus.ullTotalPhys > 0)
                {
                    profile.SystemDramGb = Math.Round(memStatus.ullTotalPhys / (1024.0 * 1024.0 * 1024.0), 1);
                }
            }
            catch
            {
            }

            // 2. Detect GPU Name & 64-bit VRAM from Windows Display Class Registry
            try
            {
                using (RegistryKey classKey = Registry.LocalMachine.OpenSubKey(@"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"))
                {
                    if (classKey != null)
                    {
                        string bestGpuName = null;
                        ulong bestVramBytes = 0;

                        foreach (string subKeyName in classKey.GetSubKeyNames())
                        {
                            if (!subKeyName.StartsWith("0"))
                            {
                                continue;
                            }
                            using (RegistryKey adapterKey = classKey.OpenSubKey(subKeyName))
                            {
                                if (adapterKey == null)
                                {
                                    continue;
                                }
                                string desc = adapterKey.GetValue("DriverDesc") as string;
                                if (string.IsNullOrWhiteSpace(desc))
                                {
                                    continue;
                                }
                                string lowerDesc = desc.ToLowerInvariant();
                                if (lowerDesc.Contains("basic display") || lowerDesc.Contains("remote") || lowerDesc.Contains("virtual"))
                                {
                                    continue;
                                }

                                ulong vramBytes = 0;
                                object qwObj = adapterKey.GetValue("HardwareInformation.qwMemorySize");
                                if (qwObj != null)
                                {
                                    if (qwObj is long)
                                    {
                                        vramBytes = (ulong)(long)qwObj;
                                    }
                                    else if (qwObj is ulong)
                                    {
                                        vramBytes = (ulong)qwObj;
                                    }
                                    else if (qwObj is byte[])
                                    {
                                        byte[] b = (byte[])qwObj;
                                        if (b.Length >= 8)
                                        {
                                            vramBytes = BitConverter.ToUInt64(b, 0);
                                        }
                                    }
                                }

                                if (vramBytes == 0)
                                {
                                    object memObj = adapterKey.GetValue("HardwareInformation.MemorySize");
                                    if (memObj is int)
                                    {
                                        vramBytes = (uint)(int)memObj;
                                    }
                                    else if (memObj is byte[])
                                    {
                                        byte[] b = (byte[])memObj;
                                        if (b.Length >= 4)
                                        {
                                            vramBytes = BitConverter.ToUInt32(b, 0);
                                        }
                                    }
                                }

                                if (vramBytes >= bestVramBytes || bestGpuName == null)
                                {
                                    bestVramBytes = vramBytes;
                                    bestGpuName = desc.Trim();
                                }
                            }
                        }

                        if (!string.IsNullOrEmpty(bestGpuName))
                        {
                            profile.GpuName = bestGpuName;
                            profile.VramGb = Math.Round(bestVramBytes / (1024.0 * 1024.0 * 1024.0), 1);
                        }
                    }
                }
            }
            catch
            {
            }

            // 3. Build human-friendly summary showing conservative usable VRAM (-2GB) and usable DRAM (*0.6)
            profile.OverallSummary = string.Format(
                "偵測到顯示卡「{0}」(原始顯存 {1:0.0} GB ➔ 扣除 2GB 後可用顯存：{2:0.0} GB) ｜ 系統記憶體 {3:0.0} GB DRAM (打 6 折後可用上限：{4:0.0} GB)。\r\n" +
                "系統已內建「中低端顯卡 + DDR4 保守速度查表引擎」，會依據您選擇模型的線上回報尺寸，自動計算顯存與 DRAM 相對填充率 (%) 並推估生成速度：",
                profile.GpuName, profile.VramGb, profile.UsableVramGb, profile.SystemDramGb, profile.UsableDramGb);

            return profile;
        }

        public void ComputeFillPercentages(double modelSizeGb, out double vramFillPct, out double dramSpillGb, out double dramFillPct, out int lookupTierIndex)
        {
            double size = Math.Max(0.1, modelSizeGb);
            double uVram = UsableVramGb;
            double uDram = Math.Max(0.1, UsableDramGb);

            if (uVram > 0.05 && size <= uVram)
            {
                vramFillPct = Math.Round((size / uVram) * 100.0, 1);
                dramSpillGb = 0.0;
                dramFillPct = 0.0;

                if (vramFillPct <= 50.0)
                {
                    lookupTierIndex = 0;
                }
                else if (vramFillPct <= 80.0)
                {
                    lookupTierIndex = 1;
                }
                else
                {
                    lookupTierIndex = 2;
                }
            }
            else
            {
                vramFillPct = 100.0;
                dramSpillGb = Math.Round(Math.Max(0.0, size - uVram), 2);
                dramFillPct = Math.Round((dramSpillGb / uDram) * 100.0, 1);

                if (dramFillPct <= 15.0)
                {
                    lookupTierIndex = 3;
                }
                else if (dramFillPct <= 35.0)
                {
                    lookupTierIndex = 4;
                }
                else if (dramFillPct <= 65.0)
                {
                    lookupTierIndex = 5;
                }
                else if (dramFillPct <= 100.0)
                {
                    lookupTierIndex = 6;
                }
                else
                {
                    lookupTierIndex = 7;
                }
            }
        }

        public string GetSpeedEvaluationForModel(string rawModelSelection)
        {
            string cleanTag = OnlineModelCatalogService.ExtractCleanModelTag(rawModelSelection);
            double modelSizeGb = OnlineModelCatalogService.ResolveModelSizeGb(rawModelSelection);

            double vramFillPct;
            double dramSpillGb;
            double dramFillPct;
            int tierIdx;
            ComputeFillPercentages(modelSizeGb, out vramFillPct, out dramSpillGb, out dramFillPct, out tierIdx);

            string tierSummary = GetLookupRowSummary(tierIdx);
            return string.Format(
                "【模型：{0} (回報尺寸 {1:0.0} GB)】可用顯存 {2:0.0}GB (填充 {3:0.0}%) ｜ 可用DRAM {4:0.0}GB (溢出 {5:0.0}GB / 填充 {6:0.0}%)\r\n" +
                "👉 保守查表推估 (中低端顯卡+DDR4)：{7}",
                cleanTag, modelSizeGb, UsableVramGb, vramFillPct, UsableDramGb, dramSpillGb, dramFillPct, tierSummary);
        }

        public static string GetLookupRowSummary(int tierIdx)
        {
            switch (tierIdx)
            {
                case 0:
                    return "⚡ 極速流暢 (~35 - 55 tokens/秒 · 約 1~2 秒秒回，100% 載入可用顯存且餘裕充足)";
                case 1:
                    return "🟢 快速流暢 (~22 - 38 tokens/秒 · 約 2~3 秒快速回覆，100% 載入中低階顯卡可用顯存)";
                case 2:
                    return "🟡 速度良好 (~15 - 25 tokens/秒 · 約 3~5 秒回覆，剛好塞入扣除 2GB 後的可用顯存)";
                case 3:
                    return "🚶 尚可接受 (~9 - 15 tokens/秒 · 每次回覆約 4~7 秒，顯存滿載並輕微借用 DDR4 記憶體 <=15%)";
                case 4:
                    return "🐢 反應偏慢 (~5 - 9 tokens/秒 · 每次回覆約 6~12 秒，中度依賴 DDR4 系統記憶體與 CPU 協同運算)";
                case 5:
                    return "🐌 明顯遲緩 (~2.5 - 5 tokens/秒 · 每次回覆約 12~25 秒，受限於 DDR4 頻寬瓶頸，建議改選更小尺寸模型)";
                case 6:
                    return "⚠️ 極度緩慢 (~0.8 - 2.5 tokens/秒 · 每次回覆需 25~60 秒以上，逼近 6 折 DRAM 上限，容易卡頓)";
                default:
                    return "⛔ 記憶體不足 (< 0.5 tokens/秒 或 OOM 崩潰 · 超出可用顯存與 6 折 DRAM 總和，請勿選擇此尺寸)";
            }
        }

        public string BuildModelComparisonTableText(string currentSelectionText)
        {
            string cleanTag = OnlineModelCatalogService.ExtractCleanModelTag(currentSelectionText);
            double modelSizeGb = OnlineModelCatalogService.ResolveModelSizeGb(currentSelectionText);

            double vramFillPct;
            double dramSpillGb;
            double dramFillPct;
            int activeTier;
            ComputeFillPercentages(modelSizeGb, out vramFillPct, out dramSpillGb, out dramFillPct, out activeTier);

            StringBuilder sb = new StringBuilder();
            sb.AppendLine(string.Format(
                "📊 【本機保守可用容量】顯卡：{0} (原始 {1:0.0}GB - 保留 2.0GB = 可用 {2:0.0}GB) ｜ 系統DRAM：原始 {3:0.0}GB × 0.6 = 可用 {4:0.0}GB",
                GpuName, VramGb, UsableVramGb, SystemDramGb, UsableDramGb));
            sb.AppendLine(string.Format(
                "🎯 【當前所選模型查表】{0} (回報尺寸 {1:0.0} GB) ➔ 可用顯存填充率: {2:0.0}%  |  可用 DRAM 溢出填充率: {3:0.0}% (溢出 {4:0.0} GB)",
                cleanTag, modelSizeGb, vramFillPct, dramFillPct, dramSpillGb));
            sb.AppendLine("----------------------------------------------------------------------------------------------------------------");
            sb.AppendLine("【內建保守速度查表 (假設中低端顯卡 + DDR4 記憶體)】             [判定條件 (VRAM / DRAM 相對填充%)]       [保守推估速度]");

            string[] tableRows = new string[]
            {
                "Tier S (⚡ 極速流暢) │ 可用顯存填充   0% ~  50%  & DRAM 溢出   0%      │ ~35 - 55 tokens/s (1~2秒秒回)",
                "Tier A (🟢 快速流暢) │ 可用顯存填充  50% ~  80%  & DRAM 溢出   0%      │ ~22 - 38 tokens/s (2~3秒快回)",
                "Tier B (🟡 速度良好) │ 可用顯存填充  80% ~ 100%  & DRAM 溢出   0%      │ ~15 - 25 tokens/s (3~5秒流暢)",
                "Tier C+(🚶 尚可接受) │ 顯存 100% 滿載 + 可用 DRAM 填充率 <= 15%        │ ~9  - 15 tokens/s (4~7秒回覆)",
                "Tier C (🐢 反應偏慢) │ 顯存 100% 滿載 + 可用 DRAM 填充率 15% ~ 35%     │ ~5  - 9  tokens/s (6~12秒回覆)",
                "Tier D (🐌 明顯遲緩) │ 顯存 100% 滿載 + 可用 DRAM 填充率 35% ~ 65%     │ ~2.5- 5  tokens/s (12~25秒遲緩)",
                "Tier E (⚠️ 極度緩慢) │ 顯存 100% 滿載 + 可用 DRAM 填充率 65% ~ 100%    │ ~0.8- 2.5tokens/s (25~60秒卡頓)",
                "Tier F (⛔ 記憶體不足)│ 超出可用顯存 + 6折DRAM總和 (DRAM 填充 > 100%)   │ OOM 無法載入或系統嚴重卡死"
            };

            for (int i = 0; i < tableRows.Length; i++)
            {
                if (i == activeTier)
                {
                    sb.AppendLine("👉 [目前落點] " + tableRows[i] + "  ◄◄ 當前所選模型");
                }
                else
                {
                    sb.AppendLine("   " + tableRows[i]);
                }
            }

            return sb.ToString().TrimEnd();
        }
    }

    /// <summary>
    /// Represents an entry in the 3-tier cascading model selector (前選單: 官方/客製, 中選單: 廠牌, 後選單: 版本與回報尺寸).
    /// </summary>
    internal sealed class ModelCatalogEntry
    {
        public string Category { get; set; } // "official" or "custom"
        public string Vendor { get; set; }   // "google", "qwen", "deepseek", "glm", "nvidia"
        public string Tag { get; set; }      // e.g. "gemma4:e2b"
        public double SizeGb { get; set; }   // e.g. 4.6
        public string Note { get; set; }     // description

        public string FormatTier3Display()
        {
            if (string.IsNullOrEmpty(Note))
            {
                return string.Format("{0}   [{1:0.0} GB]", Tag, SizeGb);
            }
            return string.Format("{0}   [{1:0.0} GB · {2}]", Tag, SizeGb, Note);
        }
    }

    /// <summary>
    /// Dynamically fetches live model lists and exact reported weight sizes (in GB) from Ollama Online Library
    /// and OCI Registry manifests, organized into 3-tier cascading menus:
    /// Tier 1 (前選單): 官方模型 (Official) / 客製與社群模型 (Custom)
    /// Tier 2 (中選單): Google / Qwen / DeepSeek / GLM / NVIDIA
    /// Tier 3 (後選單): 版本標籤與回報尺寸 (GB)
    /// </summary>
    internal static class OnlineModelCatalogService
    {
        private static readonly object SyncRoot = new object();
        private static readonly List<ModelCatalogEntry> Catalog = new List<ModelCatalogEntry>();
        private static readonly Dictionary<string, double> KnownTagSizesGb = new Dictionary<string, double>(StringComparer.OrdinalIgnoreCase);
        private static readonly HashSet<string> FetchedBuckets = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        static OnlineModelCatalogService()
        {
            InitializeSeedCatalog();
        }

        private static void InitializeSeedCatalog()
        {
            // 1. Official - Google (Gemma 4 / Gemma 3 / Gemma 3n) - verified online sizes from ollama.com
            AddOrUpdateEntry("official", "google", "gemma4:e2b", 4.6, "Gemma 4 E2B 預設多模態輕量版");
            AddOrUpdateEntry("official", "google", "gemma4:e2b-it-qat", 4.3, "Gemma 4 E2B QAT 量化提速版");
            AddOrUpdateEntry("official", "google", "gemma4:e4b", 6.6, "Gemma 4 E4B 進階平衡多模態版");
            AddOrUpdateEntry("official", "google", "gemma4:e4b-it-qat", 6.1, "Gemma 4 E4B QAT 量化提速版");
            AddOrUpdateEntry("official", "google", "gemma4:12b", 7.7, "Gemma 4 12B 中大型高智商版");
            AddOrUpdateEntry("official", "google", "gemma4:26b", 16.0, "Gemma 4 26B A4B MoE 專家混合版");
            AddOrUpdateEntry("official", "google", "gemma4:31b", 19.0, "Gemma 4 31B 旗艦推理版");
            AddOrUpdateEntry("official", "google", "gemma3:270m", 0.3, "Gemma 3 270M 超微型極速版");
            AddOrUpdateEntry("official", "google", "gemma3:1b", 0.8, "Gemma 3 1B 低顯存秒回純文字版");
            AddOrUpdateEntry("official", "google", "gemma3:4b", 3.4, "Gemma 3 4B 經典高性價比版");
            AddOrUpdateEntry("official", "google", "gemma3:12b", 8.2, "Gemma 3 12B 中高階角色扮演版");
            AddOrUpdateEntry("official", "google", "gemma3:27b", 17.0, "Gemma 3 27B 大型旗艦版");
            AddOrUpdateEntry("official", "google", "gemma3n:e2b", 7.8, "Gemma 3n E2B 原生多模態版");

            // 2. Official - Qwen (Qwen 3.5 / Qwen 3 / Qwen 3.8 / Qwen 2.5)
            AddOrUpdateEntry("official", "qwen", "qwen3.5:0.8b", 1.2, "Qwen 3.5 0.8B 超輕量極速中文版");
            AddOrUpdateEntry("official", "qwen", "qwen3.5:2b", 2.7, "Qwen 3.5 2B 輕量流暢中文版");
            AddOrUpdateEntry("official", "qwen", "qwen3.5:4b", 3.3, "Qwen 3.5 4B 平衡推理推薦版");
            AddOrUpdateEntry("official", "qwen", "qwen3.5:9b", 6.6, "Qwen 3.5 9B 進階中文角色扮演");
            AddOrUpdateEntry("official", "qwen", "qwen3.5:27b", 17.0, "Qwen 3.5 27B 高階大型版");
            AddOrUpdateEntry("official", "qwen", "qwen3:0.6b", 0.5, "Qwen 3 0.6B 微型極速版");
            AddOrUpdateEntry("official", "qwen", "qwen3:1.7b", 1.4, "Qwen 3 1.7B 低顯存中文首選");
            AddOrUpdateEntry("official", "qwen", "qwen3:4b", 2.5, "Qwen 3 4B 輕量高智商版");
            AddOrUpdateEntry("official", "qwen", "qwen3:8b", 5.2, "Qwen 3 8B 主流標準版");
            AddOrUpdateEntry("official", "qwen", "qwen3:14b", 9.3, "Qwen 3 14B 深度思考版");
            AddOrUpdateEntry("official", "qwen", "qwen3.8:27b", 18.0, "Qwen 3.8 27B 最新世代大型版");
            AddOrUpdateEntry("official", "qwen", "qwen2.5:1.5b", 1.0, "Qwen 2.5 1.5B 極速穩定版");
            AddOrUpdateEntry("official", "qwen", "qwen2.5:3b", 1.9, "Qwen 2.5 3B 2GB可用顯存完美適配");
            AddOrUpdateEntry("official", "qwen", "qwen2.5:7b", 4.7, "Qwen 2.5 7B 經典中大型版");

            // 3. Official - DeepSeek (DeepSeek R1 / V3 / Coder)
            AddOrUpdateEntry("official", "deepseek", "deepseek-r1:1.5b", 1.1, "DeepSeek R1 1.5B 極速輕量推理版");
            AddOrUpdateEntry("official", "deepseek", "deepseek-r1:7b", 4.7, "DeepSeek R1 7B 熱門蒸餾推理版");
            AddOrUpdateEntry("official", "deepseek", "deepseek-r1:8b", 5.2, "DeepSeek R1 8B Llama蒸餾推理版");
            AddOrUpdateEntry("official", "deepseek", "deepseek-r1:14b", 9.0, "DeepSeek R1 14B Qwen蒸餾進階版");
            AddOrUpdateEntry("official", "deepseek", "deepseek-r1:32b", 20.0, "DeepSeek R1 32B 高階深度推理版");
            AddOrUpdateEntry("official", "deepseek", "deepseek-coder-v2:16b", 8.9, "DeepSeek Coder V2 16B Lite MoE");

            // 4. Official - GLM (Zhipu GLM-4 / GLM-4.7-Flash)
            AddOrUpdateEntry("official", "glm", "glm4:9b-chat-q2_K", 4.0, "GLM-4 9B Q2_K 極限輕量量化版");
            AddOrUpdateEntry("official", "glm", "glm4:9b-chat-q3_K_S", 4.6, "GLM-4 9B Q3_K_S 輕量量化版");
            AddOrUpdateEntry("official", "glm", "glm4:9b-chat-q3_K_M", 5.1, "GLM-4 9B Q3_K_M 平衡量化版");
            AddOrUpdateEntry("official", "glm", "glm4:9b", 5.5, "GLM-4 9B 智譜官方標準對話版");
            AddOrUpdateEntry("official", "glm", "glm4:9b-text-q8_0", 10.0, "GLM-4 9B Q8 高精度量化版");
            AddOrUpdateEntry("official", "glm", "glm-4.7-flash:q4_K_M", 19.0, "GLM-4.7 Flash 高速推理版");

            // 5. Official - NVIDIA (Nemotron Mini / Nemotron 3 Nano)
            AddOrUpdateEntry("official", "nvidia", "nemotron-mini:4b-instruct-q2_K", 1.9, "NVIDIA Nemotron Mini 4B Q2 超輕量");
            AddOrUpdateEntry("official", "nvidia", "nemotron-mini:4b-instruct-q3_K_M", 2.3, "NVIDIA Nemotron Mini 4B Q3 平衡版");
            AddOrUpdateEntry("official", "nvidia", "nemotron-mini:4b", 2.7, "NVIDIA Nemotron Mini 4B 官方標準版");
            AddOrUpdateEntry("official", "nvidia", "nemotron-3-nano:4b", 2.8, "NVIDIA Nemotron 3 Nano 4B 最新世代");
            AddOrUpdateEntry("official", "nvidia", "nemotron-3-nano:30b", 18.0, "NVIDIA Nemotron 3 Nano 30B 大型版");

            // 6. Custom / Community - Google (Gemma Roleplay / Abliterated / Uncensored)
            AddOrUpdateEntry("custom", "google", "huihui_ai/gemma3-abliterated:1b", 0.8, "社群無限制微調 Gemma 3 1B 極速版");
            AddOrUpdateEntry("custom", "google", "huihui_ai/gemma3-abliterated:270m", 0.5, "社群無限制微調 Gemma 3 270M 超輕量");
            AddOrUpdateEntry("custom", "google", "eriktonon/gemma4-uncensored:12b", 7.6, "社群無限制角色扮演 Gemma 4 12B");
            AddOrUpdateEntry("custom", "google", "mo-shakib/gemma4-e4b-uncensored:q4_k_m", 9.6, "社群無限制微調 Gemma 4 E4B");
            AddOrUpdateEntry("custom", "google", "huihui_ai/gemma3-abliterated:4b", 8.6, "社群無限制微調 Gemma 3 4B");
            AddOrUpdateEntry("custom", "google", "eriktonon/gemma4-uncensored:26b-a4b", 15.0, "社群無限制微調 Gemma 4 26B MoE");

            // 7. Custom / Community - Qwen (Qwen Roleplay / Abliterated / Heretic)
            AddOrUpdateEntry("custom", "qwen", "huihui_ai/qwen3-abliterated:0.6b", 0.4, "社群無限制微調 Qwen 3 0.6B 超輕量");
            AddOrUpdateEntry("custom", "qwen", "huihui_ai/qwen3-abliterated:1.7b", 1.1, "社群無限制微調 Qwen 3 1.7B 角色扮演");
            AddOrUpdateEntry("custom", "qwen", "huihui_ai/qwen3-abliterated:4b", 2.5, "社群無限制微調 Qwen 3 4B 熱門RP版");
            AddOrUpdateEntry("custom", "qwen", "richardyoung/qwen3.5-4b-ara-heretic:latest", 2.7, "社群角色扮演特化 Qwen 3.5 4B Heretic");
            AddOrUpdateEntry("custom", "qwen", "huihui_ai/qwen3-abliterated:8b", 5.0, "社群無限制微調 Qwen 3 8B 進階RP版");
            AddOrUpdateEntry("custom", "qwen", "abliterant/qwen3.5-rana:latest", 6.5, "社群微調 Qwen 3.5 Rana 角色扮演版");

            // 8. Custom / Community - DeepSeek (DeepSeek R1 Abliterated / RP Distill)
            AddOrUpdateEntry("custom", "deepseek", "huihui_ai/deepseek-r1-abliterated:1.5b", 1.1, "社群無限制微調 DeepSeek R1 1.5B");
            AddOrUpdateEntry("custom", "deepseek", "huihui_ai/deepseek-r1-abliterated:7b", 4.7, "社群無限制微調 DeepSeek R1 7B");
            AddOrUpdateEntry("custom", "deepseek", "huihui_ai/deepseek-r1-abliterated:8b", 5.0, "社群無限制微調 DeepSeek R1 8B");
            AddOrUpdateEntry("custom", "deepseek", "huihui_ai/deepseek-r1-abliterated:14b", 9.0, "社群無限制微調 DeepSeek R1 14B");

            // 9. Custom / Community - GLM (Community Quantized & Long-Context GLM)
            AddOrUpdateEntry("custom", "glm", "cn/glm4:9b-32k", 5.5, "社群長文本強化 GLM-4 9B 32K");
            AddOrUpdateEntry("custom", "glm", "EntropyYue/longwriter-glm4:9b", 5.5, "社群長篇創作微調 LongWriter GLM-4 9B");

            // 10. Custom / Community - NVIDIA (Community Quantized Nemotron)
            AddOrUpdateEntry("custom", "nvidia", "dlasher/NVIDIA-Nemotron-3.5-Bolt-E4B:IQ4_NL", 6.2, "社群 IQ4 量化 Nemotron 3.5 Bolt E4B");
        }

        public static void AddOrUpdateEntry(string category, string vendor, string tag, double sizeGb, string note)
        {
            if (string.IsNullOrWhiteSpace(tag) || sizeGb <= 0.05)
            {
                return;
            }
            string cleanTag = tag.Trim();
            double roundedSize = Math.Round(sizeGb, 1);

            lock (SyncRoot)
            {
                KnownTagSizesGb[cleanTag] = roundedSize;
                for (int i = 0; i < Catalog.Count; i++)
                {
                    if (string.Equals(Catalog[i].Category, category, StringComparison.OrdinalIgnoreCase) &&
                        string.Equals(Catalog[i].Vendor, vendor, StringComparison.OrdinalIgnoreCase) &&
                        string.Equals(Catalog[i].Tag, cleanTag, StringComparison.OrdinalIgnoreCase))
                    {
                        Catalog[i].SizeGb = roundedSize;
                        if (!string.IsNullOrEmpty(note))
                        {
                            Catalog[i].Note = note;
                        }
                        return;
                    }
                }

                ModelCatalogEntry entry = new ModelCatalogEntry();
                entry.Category = category;
                entry.Vendor = vendor;
                entry.Tag = cleanTag;
                entry.SizeGb = roundedSize;
                entry.Note = note ?? "";
                Catalog.Add(entry);
            }
        }

        public static List<ModelCatalogEntry> GetEntries(string category, string vendor)
        {
            List<ModelCatalogEntry> list = new List<ModelCatalogEntry>();
            lock (SyncRoot)
            {
                foreach (ModelCatalogEntry e in Catalog)
                {
                    if (string.Equals(e.Category, category, StringComparison.OrdinalIgnoreCase) &&
                        string.Equals(e.Vendor, vendor, StringComparison.OrdinalIgnoreCase))
                    {
                        list.Add(e);
                    }
                }
            }
            list.Sort(delegate(ModelCatalogEntry a, ModelCatalogEntry b)
            {
                // Keep gemma4:e2b first in official/google if present, otherwise sort by SizeGb ascending
                if (string.Equals(a.Tag, "gemma4:e2b", StringComparison.OrdinalIgnoreCase)) return -1;
                if (string.Equals(b.Tag, "gemma4:e2b", StringComparison.OrdinalIgnoreCase)) return 1;
                return a.SizeGb.CompareTo(b.SizeGb);
            });
            return list;
        }

        public static string ExtractCleanModelTag(string rawText)
        {
            if (string.IsNullOrWhiteSpace(rawText))
            {
                return "gemma4:e2b";
            }
            string s = rawText.Trim();
            int bracketIdx = s.IndexOf('[');
            if (bracketIdx > 0)
            {
                s = s.Substring(0, bracketIdx).Trim();
            }
            int spaceIdx = s.IndexOf(' ');
            if (spaceIdx > 0)
            {
                s = s.Substring(0, spaceIdx).Trim();
            }
            return string.IsNullOrEmpty(s) ? "gemma4:e2b" : s;
        }

        public static double ResolveModelSizeGb(string rawSelectionText)
        {
            if (string.IsNullOrWhiteSpace(rawSelectionText))
            {
                return 4.6;
            }

            // 1. Check if the display string itself contains "[X.X GB"
            Match m = Regex.Match(rawSelectionText, @"\[\s*(\d+(?:\.\d+)?)\s*GB", RegexOptions.IgnoreCase);
            if (m.Success)
            {
                double parsed;
                if (double.TryParse(m.Groups[1].Value, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out parsed) && parsed > 0)
                {
                    return parsed;
                }
            }

            // 2. Check dictionary of known/fetched tags
            string cleanTag = ExtractCleanModelTag(rawSelectionText);
            lock (SyncRoot)
            {
                double known;
                if (KnownTagSizesGb.TryGetValue(cleanTag, out known) && known > 0)
                {
                    return known;
                }
            }

            // 3. Heuristic fallback from parameter size in tag name
            string lower = cleanTag.ToLowerInvariant();
            if (lower.Contains("270m") || lower.Contains("0.5b") || lower.Contains("0.6b")) return 0.5;
            if (lower.Contains("0.8b") || lower.Contains("1b")) return 0.9;
            if (lower.Contains("1.5b") || lower.Contains("1.7b")) return 1.3;
            if (lower.Contains("e2b")) return 4.6;
            if (lower.Contains("2b")) return 2.2;
            if (lower.Contains("3b")) return 2.0;
            if (lower.Contains("e4b")) return 6.6;
            if (lower.Contains("4b")) return 3.3;
            if (lower.Contains("7b")) return 4.7;
            if (lower.Contains("8b")) return 5.2;
            if (lower.Contains("9b")) return 5.5;
            if (lower.Contains("12b")) return 7.8;
            if (lower.Contains("14b") || lower.Contains("16b")) return 9.0;
            if (lower.Contains("26b") || lower.Contains("27b")) return 17.0;
            if (lower.Contains("30b") || lower.Contains("31b") || lower.Contains("32b")) return 19.5;
            return 4.0;
        }

        /// <summary>
        /// Dynamically scrapes the latest tags and reported sizes from Ollama online library / community pages
        /// for the given (category, vendor) bucket, plus queries OCI manifest if needed.
        /// </summary>
        public static int FetchOnlineCatalogForBucket(string category, string vendor, bool forceRefresh)
        {
            string bucketKey = (category + ":" + vendor).ToLowerInvariant();
            lock (SyncRoot)
            {
                if (!forceRefresh && FetchedBuckets.Contains(bucketKey))
                {
                    return GetEntries(category, vendor).Count;
                }
            }

            int newlyFound = 0;
            string[] targetTagUrls = GetOnlineTagPageUrls(category, vendor);

            foreach (string url in targetTagUrls)
            {
                try
                {
                    HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
                    req.UserAgent = "DianaSeedInstaller/2.0 (Windows NT 10.0; Win64; x64)";
                    req.Timeout = 6000;
                    req.ReadWriteTimeout = 6000;

                    using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                    using (StreamReader sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                    {
                        string html = sr.ReadToEnd();
                        // Match both /library/model:tag and /namespace/model:tag with reported size "• X.X GB|MB"
                        MatchCollection matches = Regex.Matches(
                            html,
                            @"href=""/(?:library/)?([a-zA-Z0-9._-]+(?:/[a-zA-Z0-9._-]+)?:[a-zA-Z0-9._-]+)""[\s\S]{1,950}?•\s*(\d+(?:\.\d+)?)\s*(GB|MB)",
                            RegexOptions.IgnoreCase);

                        int perRepoCount = 0;
                        foreach (Match m in matches)
                        {
                            string tag = m.Groups[1].Value.Trim();
                            string numStr = m.Groups[2].Value.Trim();
                            string unit = m.Groups[3].Value.Trim().ToUpperInvariant();

                            // Skip ":latest", ":cloud", or Apple Silicon "-mlx" tags to keep Windows list clean and accurate
                            string lowerTag = tag.ToLowerInvariant();
                            if (lowerTag.EndsWith(":latest") || lowerTag.Contains("cloud") || lowerTag.Contains("-mlx"))
                            {
                                continue;
                            }

                            double val;
                            if (!double.TryParse(numStr, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out val))
                            {
                                continue;
                            }
                            double sizeGb = (unit == "MB") ? Math.Round(val / 1024.0, 2) : Math.Round(val, 1);
                            if (sizeGb <= 0.05)
                            {
                                continue;
                            }

                            AddOrUpdateEntry(category, vendor, tag, sizeGb, "線上同步最新回報尺寸");
                            newlyFound++;
                            perRepoCount++;
                            if (perRepoCount >= 14)
                            {
                                break;
                            }
                        }
                    }
                }
                catch
                {
                }
            }

            lock (SyncRoot)
            {
                FetchedBuckets.Add(bucketKey);
            }
            return newlyFound;
        }

        /// <summary>
        /// Queries Ollama OCI Registry manifest (https://registry.ollama.ai/v2/.../manifests/...)
        /// for an arbitrary user-typed model tag to get its exact byte size in GB.
        /// </summary>
        public static double QueryLiveRegistryManifestSizeGb(string modelTag)
        {
            string cleanTag = ExtractCleanModelTag(modelTag);
            string repo = cleanTag;
            string tag = "latest";
            int colonIdx = cleanTag.IndexOf(':');
            if (colonIdx > 0)
            {
                repo = cleanTag.Substring(0, colonIdx);
                tag = cleanTag.Substring(colonIdx + 1);
            }
            if (!repo.Contains("/"))
            {
                repo = "library/" + repo;
            }

            string url = string.Format("https://registry.ollama.ai/v2/{0}/manifests/{1}", repo, tag);
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
                req.UserAgent = "DianaSeedInstaller/2.0";
                req.Timeout = 5000;
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                using (StreamReader sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                {
                    string json = sr.ReadToEnd();
                    MatchCollection matches = Regex.Matches(json, @"""size""\s*:\s*(\d+)");
                    long totalBytes = 0;
                    foreach (Match m in matches)
                    {
                        long b;
                        if (long.TryParse(m.Groups[1].Value, out b))
                        {
                            totalBytes += b;
                        }
                    }
                    if (totalBytes > 10 * 1024 * 1024)
                    {
                        double gb = Math.Round(totalBytes / 1000000000.0, 1);
                        lock (SyncRoot)
                        {
                            KnownTagSizesGb[cleanTag] = gb;
                        }
                        return gb;
                    }
                }
            }
            catch
            {
            }
            return -1.0;
        }

        private static string[] GetOnlineTagPageUrls(string category, string vendor)
        {
            bool isOfficial = string.Equals(category, "official", StringComparison.OrdinalIgnoreCase);
            string v = (vendor ?? "google").ToLowerInvariant();

            if (isOfficial)
            {
                if (v == "google")
                {
                    return new string[] { "https://ollama.com/library/gemma4/tags", "https://ollama.com/library/gemma3/tags", "https://ollama.com/library/gemma3n/tags" };
                }
                if (v == "qwen")
                {
                    return new string[] { "https://ollama.com/library/qwen3.5/tags", "https://ollama.com/library/qwen3/tags", "https://ollama.com/library/qwen2.5/tags", "https://ollama.com/library/qwen3.8/tags" };
                }
                if (v == "deepseek")
                {
                    return new string[] { "https://ollama.com/library/deepseek-r1/tags", "https://ollama.com/library/deepseek-coder-v2/tags" };
                }
                if (v == "glm")
                {
                    return new string[] { "https://ollama.com/library/glm4/tags", "https://ollama.com/library/glm-4.7-flash/tags" };
                }
                if (v == "nvidia")
                {
                    return new string[] { "https://ollama.com/library/nemotron-3-nano/tags", "https://ollama.com/library/nemotron-mini/tags" };
                }
            }
            else
            {
                if (v == "google")
                {
                    return new string[] { "https://ollama.com/eriktonon/gemma4-uncensored/tags", "https://ollama.com/huihui_ai/gemma3-abliterated/tags", "https://ollama.com/mo-shakib/gemma4-e4b-uncensored/tags" };
                }
                if (v == "qwen")
                {
                    return new string[] { "https://ollama.com/huihui_ai/qwen3-abliterated/tags", "https://ollama.com/richardyoung/qwen3.5-4b-ara-heretic/tags", "https://ollama.com/abliterant/qwen3.5-rana/tags" };
                }
                if (v == "deepseek")
                {
                    return new string[] { "https://ollama.com/huihui_ai/deepseek-r1-abliterated/tags" };
                }
                if (v == "glm")
                {
                    return new string[] { "https://ollama.com/cn/glm4/tags" };
                }
                if (v == "nvidia")
                {
                    return new string[] { "https://ollama.com/dlasher/NVIDIA-Nemotron-3.5-Bolt-E4B/tags" };
                }
            }
            return new string[0];
        }
    }

    /// <summary>
    /// Handles packing a clean, memory-free .seed archive and unpacking/initializing a cold install.
    /// </summary>
    internal static class SeedPackageEngine
    {
        private static readonly HashSet<string> ExcludedDirectories = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            ".git",
            ".vscode",
            "__pycache__",
            "diana_memory_db",
            "logs",
            "updates",
            "old",
            "venv",
            ".venv",
            "env"
        };

        private static readonly HashSet<string> ExcludedRootExtensions = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            ".seed",
            ".memory",
            ".pyc",
            ".pyo",
            ".pyd",
            ".log",
            ".tmp",
            ".pdf"
        };

        private static readonly HashSet<string> AllowedBaseImages = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "action_pull_sleeve.png",
            "action_morning_rub_eyes.jpg",
            "action_head_tilt_thinking.jpg"
        };

        public static string DefaultGuiConfigJson()
        {
            return "{\r\n" +
                   "  \"local_base_url\": \"http://127.0.0.1:11434/v1\",\r\n" +
                   "  \"local_model\": \"gemma4:e2b\",\r\n" +
                   "  \"web_base_url\": \"https://generativelanguage.googleapis.com/v1beta/openai/\",\r\n" +
                   "  \"web_model\": \"gemini-3.5-flash-lite\",\r\n" +
                   "  \"image_endpoint\": \"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image:generateContent\",\r\n" +
                   "  \"image_model\": \"gemini-3.1-flash-image\",\r\n" +
                   "  \"temperature\": 0.75,\r\n" +
                   "  \"thinking_tokens\": 256\r\n" +
                   "}\r\n";
        }

        public static string DefaultActionImagesJson()
        {
            return "[\r\n" +
                   "  {\r\n" +
                   "    \"name\": \"拉拉主人的袖子\",\r\n" +
                   "    \"keywords\": [\"拉拉主人的袖子\", \"拉主人的袖子\", \"拉拉你的袖子\", \"扯扯主人的衣角\", \"拉住主人的袖子\"],\r\n" +
                   "    \"file\": \"action_pull_sleeve.png\"\r\n" +
                   "  },\r\n" +
                   "  {\r\n" +
                   "    \"name\": \"早晨揉眼睛\",\r\n" +
                   "    \"keywords\": [\"揉揉眼睛\", \"揉眼睛\", \"揉了揉眼睛\"],\r\n" +
                   "    \"file\": \"action_morning_rub_eyes.jpg\"\r\n" +
                   "  },\r\n" +
                   "  {\r\n" +
                   "    \"name\": \"歪頭想了想\",\r\n" +
                   "    \"keywords\": [\"歪頭想了想\", \"歪頭想\", \"歪頭思考\", \"歪著頭想\"],\r\n" +
                   "    \"file\": \"action_head_tilt_thinking.jpg\"\r\n" +
                   "  }\r\n" +
                   "]\r\n";
        }

        public static string DefaultStartGuiBat()
        {
            return "@echo off\r\n" +
                   "cd /d \"%~dp0\"\r\n" +
                   "where py >nul 2>nul\r\n" +
                   "if %errorlevel%==0 (\r\n" +
                   "    py -3.12 gui.py 2>nul\r\n" +
                   "    if not errorlevel 1 exit /b 0\r\n" +
                   ")\r\n" +
                   "if exist \"%LocalAppData%\\Programs\\Python\\Python312\\python.exe\" (\r\n" +
                   "    \"%LocalAppData%\\Programs\\Python\\Python312\\python.exe\" gui.py\r\n" +
                   "    if errorlevel 1 pause\r\n" +
                   "    exit /b 0\r\n" +
                   ")\r\n" +
                   "python gui.py\r\n" +
                   "if errorlevel 1 pause\r\n";
        }

        public static int CreateCleanSeedPackage(string sourceDir, string outputSeedPath, Action<string> log)
        {
            if (string.IsNullOrWhiteSpace(sourceDir) || !Directory.Exists(sourceDir))
            {
                throw new DirectoryNotFoundException("找不到指定的專案來源目錄: " + sourceDir);
            }

            string botPyPath = Path.Combine(sourceDir, "bot.py");
            if (!File.Exists(botPyPath))
            {
                throw new InvalidOperationException("來源目錄中找不到 bot.py，請確認選擇的是有效的 Diana-Bot 專案資料夾。");
            }

            string fullSource = Path.GetFullPath(sourceDir).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string fullOutput = Path.GetFullPath(outputSeedPath);
            string outDir = Path.GetDirectoryName(fullOutput);
            if (!string.IsNullOrEmpty(outDir) && !Directory.Exists(outDir))
            {
                Directory.CreateDirectory(outDir);
            }

            string tempSeedPath = fullOutput + ".tmp";
            if (File.Exists(tempSeedPath))
            {
                File.Delete(tempSeedPath);
            }

            int addedFiles = 0;
            UTF8Encoding utf8NoBom = new UTF8Encoding(false);

            using (FileStream fs = new FileStream(tempSeedPath, FileMode.Create, FileAccess.ReadWrite, FileShare.None))
            using (ZipArchive archive = new ZipArchive(fs, ZipArchiveMode.Create, false, Encoding.UTF8))
            {
                string[] allFiles = Directory.GetFiles(fullSource, "*", SearchOption.AllDirectories);
                foreach (string filePath in allFiles)
                {
                    if (string.Equals(Path.GetFullPath(filePath), fullOutput, StringComparison.OrdinalIgnoreCase) ||
                        string.Equals(Path.GetFullPath(filePath), tempSeedPath, StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }

                    string relPath = filePath.Substring(fullSource.Length).TrimStart(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                    string[] parts = relPath.Split(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                    if (parts.Length == 0)
                    {
                        continue;
                    }

                    string topDir = parts[0];
                    if (ExcludedDirectories.Contains(topDir))
                    {
                        continue;
                    }

                    if (string.Equals(topDir, "config", StringComparison.OrdinalIgnoreCase) ||
                        string.Equals(topDir, "data", StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }

                    if (string.Equals(topDir, "img", StringComparison.OrdinalIgnoreCase))
                    {
                        if (parts.Length == 2 && !AllowedBaseImages.Contains(parts[1]))
                        {
                            log("  [略過動態生成插圖] " + relPath);
                            continue;
                        }
                    }

                    if (parts.Length == 1)
                    {
                        string ext = Path.GetExtension(filePath);
                        if (ExcludedRootExtensions.Contains(ext))
                        {
                            continue;
                        }
                        if (string.Equals(parts[0], "DianaSeedInstaller.exe", StringComparison.OrdinalIgnoreCase))
                        {
                            continue;
                        }
                    }

                    string zipEntryName = relPath.Replace('\\', '/');
                    AddFileToZip(archive, filePath, zipEntryName);
                    addedFiles++;
                    log("  + 封裝系統檔: " + zipEntryName);
                }

                string examplePersonaPath = Path.Combine(fullSource, "config", "persona_diana_example.json");
                string activePersonaPath = Path.Combine(fullSource, "config", "character_persona.json");
                string cleanPersonaJson = null;

                if (File.Exists(examplePersonaPath))
                {
                    cleanPersonaJson = File.ReadAllText(examplePersonaPath, Encoding.UTF8);
                }
                else if (File.Exists(activePersonaPath))
                {
                    cleanPersonaJson = File.ReadAllText(activePersonaPath, Encoding.UTF8);
                }

                if (!string.IsNullOrEmpty(cleanPersonaJson))
                {
                    AddTextToZip(archive, "config/persona_diana_example.json", cleanPersonaJson, utf8NoBom);
                    AddTextToZip(archive, "config/character_persona.json", cleanPersonaJson, utf8NoBom);
                    addedFiles += 2;
                    log("  + 封裝純淨角色設定 (已重置個人記憶特徵): config/character_persona.json");
                }

                AddTextToZip(archive, "config/action_images.json", DefaultActionImagesJson(), utf8NoBom);
                AddTextToZip(archive, "config/gui_config.json", DefaultGuiConfigJson(), utf8NoBom);
                addedFiles += 2;
                log("  + 封裝預設連線與插圖設定: config/gui_config.json, config/action_images.json");
                log("  [安全過濾] 已排除私人金鑰: discord_token.txt, google_api_key_*.txt");

                string versionPath = Path.Combine(fullSource, "data", "version.json");
                string versionJson = File.Exists(versionPath)
                    ? File.ReadAllText(versionPath, Encoding.UTF8)
                    : "{\r\n  \"bot\": \"2608.11\",\r\n  \"diana_addons\": \"2611.1\",\r\n  \"character_persona\": \"2608.6\"\r\n}\r\n";

                AddTextToZip(archive, "data/version.json", versionJson, utf8NoBom);
                AddTextToZip(archive, "data/reminders.json", "[]\r\n", utf8NoBom);
                addedFiles += 2;
                log("  + 封裝純淨資料結構: data/version.json, data/reminders.json");
                log("  [無記憶保證] 已排除記憶與主人綁定: data/memory.json, data/base.json, data/favorability.json, data/user_profile.json, diana_memory_db/");

                string manifestJson = "{\r\n" +
                                      "  \"package_format\": \"diana-bot-seed-v1\",\r\n" +
                                      "  \"memory_free\": true,\r\n" +
                                      "  \"cold_start_unbound\": true,\r\n" +
                                      "  \"recommended_python\": \"3.12\",\r\n" +
                                      "  \"recommended_local_server\": \"ollama\",\r\n" +
                                      "  \"recommended_local_model\": \"gemma4:e2b\",\r\n" +
                                      "  \"created_at\": \"" + DateTime.Now.ToString("yyyy-MM-ddTHH:mm:sszzz") + "\"\r\n" +
                                      "}\r\n";
                AddTextToZip(archive, "seed_manifest.json", manifestJson, utf8NoBom);
                addedFiles++;
            }

            if (File.Exists(fullOutput))
            {
                File.Delete(fullOutput);
            }
            File.Move(tempSeedPath, fullOutput);
            return addedFiles;
        }

        public static int ExtractSeedPackage(string seedPath, string targetDir, Action<string> log)
        {
            if (string.IsNullOrWhiteSpace(seedPath) || !File.Exists(seedPath))
            {
                throw new FileNotFoundException("找不到指定的 .seed 安裝包檔案: " + seedPath);
            }

            if (!Directory.Exists(targetDir))
            {
                Directory.CreateDirectory(targetDir);
            }

            int extractedCount = 0;
            UTF8Encoding utf8NoBom = new UTF8Encoding(false);

            using (FileStream fs = new FileStream(seedPath, FileMode.Open, FileAccess.Read, FileShare.Read))
            using (ZipArchive archive = new ZipArchive(fs, ZipArchiveMode.Read))
            {
                foreach (ZipArchiveEntry entry in archive.Entries)
                {
                    if (string.IsNullOrEmpty(entry.Name) && entry.FullName.EndsWith("/"))
                    {
                        string dirPath = Path.Combine(targetDir, entry.FullName.Replace('/', Path.DirectorySeparatorChar));
                        if (!Directory.Exists(dirPath))
                        {
                            Directory.CreateDirectory(dirPath);
                        }
                        continue;
                    }

                    string normalizedRel = entry.FullName.Replace('/', Path.DirectorySeparatorChar).TrimStart(Path.DirectorySeparatorChar);
                    string destPath = Path.GetFullPath(Path.Combine(targetDir, normalizedRel));
                    string fullTargetRoot = Path.GetFullPath(targetDir).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
                    if (!destPath.StartsWith(fullTargetRoot, StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }

                    if (IsMemoryOrSecretRelativePath(normalizedRel))
                    {
                        log("  [安全跳過] 忽略記憶或私密檔案: " + normalizedRel);
                        continue;
                    }

                    string parentDir = Path.GetDirectoryName(destPath);
                    if (!string.IsNullOrEmpty(parentDir) && !Directory.Exists(parentDir))
                    {
                        Directory.CreateDirectory(parentDir);
                    }

                    entry.ExtractToFile(destPath, true);
                    extractedCount++;
                }
            }

            InitializeCleanTargetEnvironment(targetDir, utf8NoBom);
            return extractedCount;
        }

        /// <summary>
        /// Extracts a GitHub repository archive ZIP (stripping the top-level repo-branch/ folder)
        /// and initializes the clean memory-free environment.
        /// </summary>
        public static int ExtractGitHubArchive(string zipPath, string targetDir, Action<string> log)
        {
            if (string.IsNullOrWhiteSpace(zipPath) || !File.Exists(zipPath))
            {
                throw new FileNotFoundException("找不到下載的 GitHub 壓縮包檔案: " + zipPath);
            }

            if (!Directory.Exists(targetDir))
            {
                Directory.CreateDirectory(targetDir);
            }

            int extractedCount = 0;
            UTF8Encoding utf8NoBom = new UTF8Encoding(false);

            using (FileStream fs = new FileStream(zipPath, FileMode.Open, FileAccess.Read, FileShare.Read))
            using (ZipArchive archive = new ZipArchive(fs, ZipArchiveMode.Read))
            {
                foreach (ZipArchiveEntry entry in archive.Entries)
                {
                    string rawFullName = entry.FullName.Replace('\\', '/').TrimStart('/');
                    int slashIdx = rawFullName.IndexOf('/');
                    if (slashIdx < 0)
                    {
                        continue;
                    }
                    // Strip top-level GitHub archive prefix (e.g. "Diana-Bot-main/")
                    string strippedRel = rawFullName.Substring(slashIdx + 1).TrimStart('/');
                    if (string.IsNullOrEmpty(strippedRel))
                    {
                        continue;
                    }

                    if (string.IsNullOrEmpty(entry.Name) && strippedRel.EndsWith("/"))
                    {
                        string dirPath = Path.Combine(targetDir, strippedRel.Replace('/', Path.DirectorySeparatorChar));
                        if (!Directory.Exists(dirPath))
                        {
                            Directory.CreateDirectory(dirPath);
                        }
                        continue;
                    }

                    string normalizedRel = strippedRel.Replace('/', Path.DirectorySeparatorChar);
                    string destPath = Path.GetFullPath(Path.Combine(targetDir, normalizedRel));
                    string fullTargetRoot = Path.GetFullPath(targetDir).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
                    if (!destPath.StartsWith(fullTargetRoot, StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }

                    if (IsMemoryOrSecretRelativePath(normalizedRel))
                    {
                        continue;
                    }

                    string parentDir = Path.GetDirectoryName(destPath);
                    if (!string.IsNullOrEmpty(parentDir) && !Directory.Exists(parentDir))
                    {
                        Directory.CreateDirectory(parentDir);
                    }

                    entry.ExtractToFile(destPath, true);
                    extractedCount++;
                }
            }

            InitializeCleanTargetEnvironment(targetDir, utf8NoBom);
            return extractedCount;
        }

        private static void InitializeCleanTargetEnvironment(string targetDir, UTF8Encoding utf8NoBom)
        {
            string configDir = Path.Combine(targetDir, "config");
            string dataDir = Path.Combine(targetDir, "data");
            string logsDir = Path.Combine(targetDir, "logs");
            string imgRefDir = Path.Combine(targetDir, "img", "reference");
            Directory.CreateDirectory(configDir);
            Directory.CreateDirectory(dataDir);
            Directory.CreateDirectory(logsDir);
            Directory.CreateDirectory(imgRefDir);

            string[] memoryFilesToClear = new string[]
            {
                Path.Combine(dataDir, "base.json"),
                Path.Combine(dataDir, "memory.json"),
                Path.Combine(dataDir, "favorability.json"),
                Path.Combine(dataDir, "guest_notes.json"),
                Path.Combine(dataDir, "reminders.json"),
                Path.Combine(dataDir, "user_profile.json"),
                Path.Combine(dataDir, "bot_state.json"),
                Path.Combine(dataDir, "runtime_status.json")
            };
            foreach (string mf in memoryFilesToClear)
            {
                if (File.Exists(mf))
                {
                    File.Delete(mf);
                }
            }

            string chromaDir = Path.Combine(targetDir, "diana_memory_db");
            if (Directory.Exists(chromaDir))
            {
                try
                {
                    Directory.Delete(chromaDir, true);
                }
                catch
                {
                }
            }

            // Ensure default configs exist even when installing from GitHub repo
            string examplePersona = Path.Combine(configDir, "persona_diana_example.json");
            string activePersona = Path.Combine(configDir, "character_persona.json");
            if (!File.Exists(activePersona) && File.Exists(examplePersona))
            {
                File.Copy(examplePersona, activePersona, true);
            }

            string guiCfg = Path.Combine(configDir, "gui_config.json");
            if (!File.Exists(guiCfg))
            {
                File.WriteAllText(guiCfg, DefaultGuiConfigJson(), utf8NoBom);
            }

            string actImg = Path.Combine(configDir, "action_images.json");
            if (!File.Exists(actImg))
            {
                File.WriteAllText(actImg, DefaultActionImagesJson(), utf8NoBom);
            }

            string remFile = Path.Combine(dataDir, "reminders.json");
            if (!File.Exists(remFile))
            {
                File.WriteAllText(remFile, "[]\r\n", utf8NoBom);
            }

            string verFile = Path.Combine(dataDir, "version.json");
            if (!File.Exists(verFile))
            {
                File.WriteAllText(verFile, "{\r\n  \"bot\": \"2608.11\",\r\n  \"diana_addons\": \"2611.1\",\r\n  \"character_persona\": \"2608.6\"\r\n}\r\n", utf8NoBom);
            }

            string[] keyFiles = new string[]
            {
                "discord_token.txt",
                "google_api_key_chat.txt",
                "google_api_key_evolution.txt",
                "google_api_key_image.txt"
            };
            foreach (string kf in keyFiles)
            {
                string kp = Path.Combine(configDir, kf);
                if (!File.Exists(kp))
                {
                    File.WriteAllText(kp, "", utf8NoBom);
                }
            }

            string startBatPath = Path.Combine(targetDir, "start_gui.bat");
            File.WriteAllText(startBatPath, DefaultStartGuiBat(), Encoding.ASCII);
        }

        public static void UpdateGuiConfigForLocalAI(string targetDir, string localBaseUrl, string localModel, Action<string> log)
        {
            string configDir = Path.Combine(targetDir, "config");
            Directory.CreateDirectory(configDir);
            string guiConfigPath = Path.Combine(configDir, "gui_config.json");
            UTF8Encoding utf8NoBom = new UTF8Encoding(false);

            string json = File.Exists(guiConfigPath)
                ? File.ReadAllText(guiConfigPath, Encoding.UTF8)
                : DefaultGuiConfigJson();

            json = ReplaceJsonStringField(json, "local_base_url", localBaseUrl);
            json = ReplaceJsonStringField(json, "local_model", localModel);

            File.WriteAllText(guiConfigPath, json, utf8NoBom);
            log(string.Format("  ✓ 已更新 config/gui_config.json -> local_base_url=\"{0}\", local_model=\"{1}\"", localBaseUrl, localModel));
        }

        private static string ReplaceJsonStringField(string json, string key, string newValue)
        {
            string escapedVal = (newValue ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"");
            string pattern = "(\"" + Regex.Escape(key) + "\"\\s*:\\s*\")([^\"]*)(\")";
            if (Regex.IsMatch(json, pattern))
            {
                return Regex.Replace(json, pattern, "${1}" + escapedVal + "${3}");
            }
            return json;
        }

        private static bool IsMemoryOrSecretRelativePath(string relPath)
        {
            string norm = relPath.Replace('\\', '/').TrimStart('/');
            if (norm.StartsWith("diana_memory_db/", StringComparison.OrdinalIgnoreCase) ||
                norm.StartsWith("logs/", StringComparison.OrdinalIgnoreCase) ||
                norm.StartsWith("updates/", StringComparison.OrdinalIgnoreCase) ||
                norm.StartsWith("old/", StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            string[] blockedFiles = new string[]
            {
                "data/base.json",
                "data/memory.json",
                "data/favorability.json",
                "data/guest_notes.json",
                "data/reminders.json",
                "data/user_profile.json",
                "data/bot_state.json",
                "data/runtime_status.json",
                "config/discord_token.txt",
                "config/google_api_key_chat.txt",
                "config/google_api_key_evolution.txt",
                "config/google_api_key_image.txt"
            };
            foreach (string bf in blockedFiles)
            {
                if (string.Equals(norm, bf, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
            return false;
        }

        private static void AddFileToZip(ZipArchive archive, string sourceFilePath, string entryName)
        {
            ZipArchiveEntry entry = archive.CreateEntry(entryName, CompressionLevel.Optimal);
            entry.LastWriteTime = File.GetLastWriteTime(sourceFilePath);
            using (FileStream input = new FileStream(sourceFilePath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (Stream output = entry.Open())
            {
                input.CopyTo(output);
            }
        }

        private static void AddTextToZip(ZipArchive archive, string entryName, string content, Encoding encoding)
        {
            ZipArchiveEntry entry = archive.CreateEntry(entryName, CompressionLevel.Optimal);
            entry.LastWriteTime = DateTime.Now;
            byte[] bytes = encoding.GetBytes(content);
            using (Stream output = entry.Open())
            {
                output.Write(bytes, 0, bytes.Length);
            }
        }
    }

    /// <summary>
    /// Freely resizable, DPI-adaptive Multi-Step Wizard Installer Form.
    /// Page 0: Mode Selection (1. 簡易完整安裝 [預設給不懂電腦的新手] / 2. 自訂安裝 [★ 推薦選項，給懂電腦的用戶逐頁設定])
    /// Page 1: Express Mode Hardware & Model Speed Guidance Page
    /// Page 2: Custom Step 1 - Paths & Core .seed Package
    /// Page 3: Custom Step 2 - Python Environment & Dependencies
    /// Page 4: Custom Step 3 - Optional Local AI Server (Ollama / LM Studio / Skip) & Hardware Speed Guidance
    /// Page 5: Installation Progress & Live Log Console
    /// Page 6: Post-Install Discord Bot API Token Guide & Quick-Save Setup
    /// </summary>
    internal sealed class InstallerWizardForm : Form
    {
        private static readonly Color BgApp = Color.FromArgb(11, 15, 25);
        private static readonly Color BgCard = Color.FromArgb(21, 29, 46);
        private static readonly Color BgCardSelected = Color.FromArgb(28, 39, 64);
        private static readonly Color BgInput = Color.FromArgb(15, 23, 42);
        private static readonly Color BorderColor = Color.FromArgb(42, 56, 86);
        private static readonly Color AccentColor = Color.FromArgb(99, 102, 241);
        private static readonly Color SuccessColor = Color.FromArgb(16, 185, 129);
        private static readonly Color WarningColor = Color.FromArgb(245, 158, 11);
        private static readonly Color DangerColor = Color.FromArgb(239, 68, 68);
        private static readonly Color TextPrimary = Color.FromArgb(241, 245, 249);
        private static readonly Color TextSecondary = Color.FromArgb(148, 163, 184);

        private HardwareProfile hwProfile;
        private int currentPage = 0;
        private bool isRunningTask = false;
        private bool isExpressModeSelected = true;

        // Top Header & Bottom Footer
        private Label lblHeaderTitle;
        private Label lblHeaderStepBadge;
        private Label lblHeaderHardware;
        private Panel pageHostPanel;
        private Button btnBack;
        private Button btnNext;
        private Button btnPackSeedFooter;

        // Wizard Pages
        private Panel[] pages;

        // Page 0: Mode Selection Cards (Direct right-side jump buttons)
        private Panel cardExpress;
        private Panel cardCustom;

        // Page 1: Express Mode Hardware & 3-Tier Dynamic Model Selector
        private TextBox txtExpressSeedPath;
        private TextBox txtExpressModelTable;
        private ComboBox cmbExpressTier1Category;
        private ComboBox cmbExpressTier2Vendor;
        private ComboBox cmbExpressModel;
        private Label lblExpressOnlineStatus;
        private Label lblExpressSpeedDynamic;

        // Page 2: Custom Step 1 (Paths & Core Seed / GitHub Preview)
        private RadioButton rbCustomSourceSeed;
        private RadioButton rbCustomSourceGitHub;
        private ComboBox cmbGitHubBranch;
        private TextBox txtSeedPath;
        private TextBox txtTargetDir;
        private CheckBox chkCustomSeed;
        private CheckBox chkCustomShortcut;
        private CheckBox chkCustomLaunchGui;

        // Page 3: Custom Step 2 (Python 3.12)
        private Label lblDetectedPythonInfo;
        private CheckBox chkCustomPython;
        private ComboBox cmbPythonVersion;
        private CheckBox chkCustomPipDeps;
        private CheckBox chkForcePython;

        // Page 4: Custom Step 3 (Optional Local AI Server & 3-Tier Dynamic Model Selector)
        private RadioButton rbLocalOllama;
        private RadioButton rbLocalLmStudio;
        private RadioButton rbLocalNone;
        private ComboBox cmbCustomTier1Category;
        private ComboBox cmbCustomTier2Vendor;
        private ComboBox cmbCustomModel;
        private Label lblCustomOnlineStatus;
        private Label lblCustomSpeedDynamic;
        private TextBox txtCustomTable;

        // Page 5: Progress & Live Console
        private Label lblInstallStatus;
        private ProgressBar progressBar;
        private RichTextBox txtLog;
        private Button btnViewDiscordGuide;

        // Page 6: Post-Install Discord Bot API Guide & Quick Token Save
        private TextBox txtQuickDiscordToken;
        private TextBox txtQuickGeminiKey;
        private Label lblQuickSaveResult;

        public InstallerWizardForm()
        {
            this.hwProfile = HardwareProfile.Detect();
            InitializeWizardLayout();
            InitializeDefaultValues();
            ShowWizardPage(0);
            this.Shown += delegate
            {
                ShowWizardPage(currentPage);
            };
        }

        private void InitializeWizardLayout()
        {
            this.Text = "Diana-Bot 冷安裝與無記憶種子部署精靈 (Cold-Install Wizard v2.1)";
            this.FormBorderStyle = FormBorderStyle.Sizable;
            this.MaximizeBox = true;
            this.MinimizeBox = true;
            this.AutoScaleMode = AutoScaleMode.Dpi;
            this.BackColor = BgApp;
            this.ForeColor = TextPrimary;
            this.Font = new Font("Microsoft JhengHei UI", 9.75f, FontStyle.Regular);

            // Dynamic initial size based on screen working area (freely resizable!)
            Rectangle workArea = Screen.PrimaryScreen.WorkingArea;
            int initW = Math.Max(820, Math.Min(1120, (int)(workArea.Width * 0.75)));
            int initH = Math.Max(600, Math.Min(850, (int)(workArea.Height * 0.80)));
            this.Size = new Size(initW, initH);
            this.MinimumSize = new Size(720, 500);
            this.StartPosition = FormStartPosition.CenterScreen;

            // Main Root Table (3 Rows: Header, Scrollable Page Host, Footer Navigation)
            TableLayoutPanel rootTable = new TableLayoutPanel();
            rootTable.Dock = DockStyle.Fill;
            rootTable.ColumnCount = 1;
            rootTable.RowCount = 3;
            rootTable.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            rootTable.RowStyles.Add(new RowStyle(SizeType.Percent, 100f));
            rootTable.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            this.Controls.Add(rootTable);

            // 1. Top Header Bar
            Panel topBar = new Panel();
            topBar.Dock = DockStyle.Top;
            topBar.AutoSize = true;
            topBar.BackColor = BgCard;
            topBar.Padding = new Padding(20, 14, 20, 14);

            TableLayoutPanel headerGrid = new TableLayoutPanel();
            headerGrid.Dock = DockStyle.Top;
            headerGrid.AutoSize = true;
            headerGrid.ColumnCount = 2;
            headerGrid.RowCount = 2;
            headerGrid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 65f));
            headerGrid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 35f));

            lblHeaderTitle = new Label();
            lblHeaderTitle.Text = "🌱 Diana-Bot 冷安裝部署精靈";
            lblHeaderTitle.Font = new Font("Microsoft JhengHei UI", 14f, FontStyle.Bold);
            lblHeaderTitle.ForeColor = TextPrimary;
            lblHeaderTitle.AutoSize = true;
            lblHeaderTitle.Anchor = AnchorStyles.Left | AnchorStyles.Top;
            headerGrid.Controls.Add(lblHeaderTitle, 0, 0);

            lblHeaderStepBadge = new Label();
            lblHeaderStepBadge.Text = "步驟 1 · 選擇安裝模式";
            lblHeaderStepBadge.Font = new Font("Microsoft JhengHei UI", 10f, FontStyle.Bold);
            lblHeaderStepBadge.ForeColor = AccentColor;
            lblHeaderStepBadge.AutoSize = true;
            lblHeaderStepBadge.Anchor = AnchorStyles.Right | AnchorStyles.Top;
            headerGrid.Controls.Add(lblHeaderStepBadge, 1, 0);

            lblHeaderHardware = new Label();
            lblHeaderHardware.Text = string.Format(
                "💻 已偵測本機硬體：{0} (原始 {1:0.0}GB ➔ 扣2GB可用顯存 {2:0.0}GB) · 系統DRAM {3:0.0}GB (打6折可用 {4:0.0}GB)",
                hwProfile.GpuName, hwProfile.VramGb, hwProfile.UsableVramGb, hwProfile.SystemDramGb, hwProfile.UsableDramGb);
            lblHeaderHardware.Font = new Font("Microsoft JhengHei UI", 9f, FontStyle.Regular);
            lblHeaderHardware.ForeColor = TextSecondary;
            lblHeaderHardware.AutoSize = true;
            lblHeaderHardware.Margin = new Padding(0, 4, 0, 0);
            headerGrid.SetColumnSpan(lblHeaderHardware, 2);
            headerGrid.Controls.Add(lblHeaderHardware, 0, 1);

            topBar.Controls.Add(headerGrid);
            rootTable.Controls.Add(topBar, 0, 0);

            // 2. Center Scrollable Page Host
            pageHostPanel = new Panel();
            pageHostPanel.Dock = DockStyle.Fill;
            pageHostPanel.BackColor = BgApp;
            pageHostPanel.Padding = new Padding(20, 14, 20, 14);
            rootTable.Controls.Add(pageHostPanel, 0, 1);

            // 3. Bottom Navigation Bar
            Panel footerBar = new Panel();
            footerBar.Dock = DockStyle.Bottom;
            footerBar.AutoSize = true;
            footerBar.BackColor = BgCard;
            footerBar.Padding = new Padding(20, 12, 20, 12);

            TableLayoutPanel footerGrid = new TableLayoutPanel();
            footerGrid.Dock = DockStyle.Top;
            footerGrid.AutoSize = true;
            footerGrid.ColumnCount = 3;
            footerGrid.RowCount = 1;
            footerGrid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));
            footerGrid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            footerGrid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

            btnPackSeedFooter = CreateActionButton("🌱 封裝目前專案為 .seed", BgInput, TextSecondary, 210, 38);
            btnPackSeedFooter.Anchor = AnchorStyles.Left;
            btnPackSeedFooter.Click += OnPackSeedClick;
            footerGrid.Controls.Add(btnPackSeedFooter, 0, 0);

            btnBack = CreateActionButton("⬅ 上一步", BgInput, TextPrimary, 130, 38);
            btnBack.Margin = new Padding(0, 0, 10, 0);
            btnBack.Click += OnBackButtonClick;
            footerGrid.Controls.Add(btnBack, 1, 0);

            btnNext = CreateActionButton("下一步 ➡", AccentColor, Color.White, 190, 38);
            btnNext.Click += OnNextButtonClick;
            footerGrid.Controls.Add(btnNext, 2, 0);

            footerBar.Controls.Add(footerGrid);
            rootTable.Controls.Add(footerBar, 0, 2);

            // Build all 7 wizard pages inside scrollable containers
            pages = new Panel[7];
            pages[0] = BuildPage0ModeSelection();
            pages[1] = BuildPage1ExpressHardwareAndModel();
            pages[2] = BuildPage2CustomPaths();
            pages[3] = BuildPage3CustomPython();
            pages[4] = BuildPage4CustomLocalAi();
            pages[5] = BuildPage5ExecutionProgress();
            pages[6] = BuildPage6PostInstallGuide();

            for (int i = 0; i < pages.Length; i++)
            {
                pages[i].Dock = DockStyle.Fill;
                pages[i].Visible = false;
                pageHostPanel.Controls.Add(pages[i]);
            }
        }

        // -------------------------------------------------------------------
        // Page Builders (100% Flow/Table Adaptive, AutoScroll enabled)
        // -------------------------------------------------------------------

        private Panel CreateScrollablePageContainer(out TableLayoutPanel stack)
        {
            Panel scrollHost = new Panel();
            scrollHost.Dock = DockStyle.Fill;
            scrollHost.AutoScroll = true;
            scrollHost.BackColor = BgApp;

            TableLayoutPanel tlp = new TableLayoutPanel();
            tlp.Dock = DockStyle.Top;
            tlp.AutoSize = true;
            tlp.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            tlp.ColumnCount = 1;
            tlp.RowCount = 0;
            tlp.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));
            tlp.Padding = new Padding(4, 2, 12, 12);

            scrollHost.Controls.Add(tlp);
            scrollHost.Resize += delegate
            {
                ReflowScrollablePage(scrollHost, tlp);
            };
            stack = tlp;
            return scrollHost;
        }

        private void ReflowScrollablePage(Panel scrollHost, TableLayoutPanel tlp)
        {
            if (scrollHost.ClientSize.Width <= 100)
            {
                return;
            }
            int availWidth = Math.Max(420, scrollHost.ClientSize.Width - SystemInformation.VerticalScrollBarWidth - 8);
            int innerMaxTextWidth = Math.Max(340, availWidth - 56);
            tlp.SuspendLayout();
            tlp.MaximumSize = new Size(availWidth, 0);
            tlp.MinimumSize = new Size(availWidth, 0);
            UpdateWrappingBoundsRecursive(tlp, innerMaxTextWidth);
            tlp.ResumeLayout(true);
        }

        private void UpdateWrappingBoundsRecursive(Control parent, int maxTextWidth)
        {
            foreach (Control c in parent.Controls)
            {
                if ((c is Label || c is RadioButton || c is CheckBox) && c.AutoSize)
                {
                    if (string.Equals(c.Tag as string, "CardLeftText", StringComparison.Ordinal))
                    {
                        c.MaximumSize = new Size(Math.Max(220, maxTextWidth - 245), 0);
                    }
                    else
                    {
                        c.MaximumSize = new Size(maxTextWidth, 0);
                    }
                }
                if (c.HasChildren)
                {
                    UpdateWrappingBoundsRecursive(c, maxTextWidth);
                }
            }
        }

        private void AddStackRow(TableLayoutPanel stack, Control ctrl, int bottomMargin)
        {
            int rowIdx = stack.RowCount;
            stack.RowCount = rowIdx + 1;
            stack.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            ctrl.Dock = DockStyle.Top;
            ctrl.Margin = new Padding(0, 0, 0, bottomMargin);
            stack.Controls.Add(ctrl, 0, rowIdx);
        }

        private Panel CreateCardBox(bool highlighted)
        {
            Panel card = new Panel();
            card.AutoSize = true;
            card.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            card.BackColor = highlighted ? BgCardSelected : BgCard;
            card.Padding = new Padding(18, 16, 18, 16);
            card.BorderStyle = BorderStyle.FixedSingle;
            return card;
        }

        private Label CreateWrappingLabel(string text, Font font, Color color, int topMargin, int bottomMargin)
        {
            Label lbl = new Label();
            lbl.Text = text;
            lbl.Font = font;
            lbl.ForeColor = color;
            lbl.AutoSize = true;
            lbl.Dock = DockStyle.Top;
            lbl.Margin = new Padding(0, topMargin, 0, bottomMargin);
            return lbl;
        }

        private Button CreateActionButton(string text, Color bg, Color fg, int minWidth, int height)
        {
            Button b = new Button();
            b.Text = text;
            b.BackColor = bg;
            b.ForeColor = fg;
            b.FlatStyle = FlatStyle.Flat;
            b.FlatAppearance.BorderColor = BorderColor;
            b.FlatAppearance.BorderSize = 1;
            b.Font = new Font("Microsoft JhengHei UI", 9.75f, FontStyle.Bold);
            b.AutoSize = true;
            b.MinimumSize = new Size(minWidth, height);
            b.Padding = new Padding(10, 4, 10, 4);
            b.Cursor = Cursors.Hand;
            return b;
        }

        /// <summary>
        /// Page 0: Displays 1. Express Install and 2. Custom Install cards, each with an independent
        /// direct jump button on the right side of the description window (no radio buttons + Next).
        /// </summary>
        private Panel BuildPage0ModeSelection()
        {
            TableLayoutPanel stack;
            Panel page = CreateScrollablePageContainer(out stack);

            Label introLbl = CreateWrappingLabel(
                "歡迎使用 Diana-Bot 冷安裝精靈！請直接點擊說明視窗右側的獨立按鈕進入「簡易完整安裝」或「自訂安裝」：",
                new Font("Microsoft JhengHei UI", 11f, FontStyle.Bold),
                TextPrimary, 0, 14);
            AddStackRow(stack, introLbl, 12);

            // Option 1 Card: Express Full Install (Left: Description, Right: Independent Jump Button)
            cardExpress = CreateCardBox(false);
            TableLayoutPanel expGrid = new TableLayoutPanel();
            expGrid.Dock = DockStyle.Top;
            expGrid.AutoSize = true;
            expGrid.ColumnCount = 2;
            expGrid.RowCount = 1;
            expGrid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));
            expGrid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

            TableLayoutPanel expLeftStack = new TableLayoutPanel();
            expLeftStack.Dock = DockStyle.Top;
            expLeftStack.AutoSize = true;
            expLeftStack.ColumnCount = 1;
            expLeftStack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));

            Label lblExpTitle = CreateWrappingLabel(
                "1. 簡易完整安裝（預設新手選項 · 適合完全不懂電腦的用戶，無腦全選所有依賴項）",
                new Font("Microsoft JhengHei UI", 11.5f, FontStyle.Bold),
                SuccessColor, 0, 8);
            lblExpTitle.Tag = "CardLeftText";
            AddStackRow(expLeftStack, lblExpTitle, 6);

            Label expDesc = CreateWrappingLabel(
                "• 【強制使用 .seed 種子包】自動將無記憶系統包 (.seed) 安裝至 C:\\Diana-Bot，並建立桌面捷徑。\r\n" +
                "• 自動檢測並安裝 Python 3.12 執行環境與所有必要套件 (requirements.txt)。\r\n" +
                "• 自動安裝本地 AI 伺服端 (Ollama)，並在下一頁從線上抓取模型列表、依扣除 2GB 後顯存與 6 折 DRAM 查表推估速度。\r\n" +
                "• 安裝完成後自動顯示「如何取得 Discord Bot API Token 與綁定主人」的新手圖文步驟指引！",
                new Font("Microsoft JhengHei UI", 9.75f, FontStyle.Regular),
                TextPrimary, 0, 2);
            expDesc.Tag = "CardLeftText";
            AddStackRow(expLeftStack, expDesc, 0);

            Button btnJumpExpress = CreateActionButton("⚡ 進入簡易安裝 ➡", SuccessColor, Color.White, 215, 64);
            btnJumpExpress.Font = new Font("Microsoft JhengHei UI", 10.75f, FontStyle.Bold);
            btnJumpExpress.Anchor = AnchorStyles.Right;
            btnJumpExpress.Margin = new Padding(16, 8, 0, 8);
            btnJumpExpress.Click += delegate
            {
                isExpressModeSelected = true;
                ShowWizardPage(1);
            };

            expGrid.Controls.Add(expLeftStack, 0, 0);
            expGrid.Controls.Add(btnJumpExpress, 1, 0);
            cardExpress.Controls.Add(expGrid);
            AddStackRow(stack, cardExpress, 18);

            // Option 2 Card: Custom Step-by-Step Install (Left: Description, Right: Independent Jump Button)
            cardCustom = CreateCardBox(true);
            TableLayoutPanel custGrid = new TableLayoutPanel();
            custGrid.Dock = DockStyle.Top;
            custGrid.AutoSize = true;
            custGrid.ColumnCount = 2;
            custGrid.RowCount = 1;
            custGrid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));
            custGrid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

            TableLayoutPanel custLeftStack = new TableLayoutPanel();
            custLeftStack.Dock = DockStyle.Top;
            custLeftStack.AutoSize = true;
            custLeftStack.ColumnCount = 1;
            custLeftStack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));

            Label lblCustTitle = CreateWrappingLabel(
                "2. 自訂安裝（★ 推薦選項 · 適合懂電腦的用戶，逐頁自行設定安裝細項）",
                new Font("Microsoft JhengHei UI", 11.5f, FontStyle.Bold),
                AccentColor, 0, 8);
            lblCustTitle.Tag = "CardLeftText";
            AddStackRow(custLeftStack, lblCustTitle, 6);

            Label custDesc = CreateWrappingLabel(
                "• 系統來源可自由選擇：「使用本地無記憶 .seed 檔」或「🌐 預覽版：直接從 GitHub (Ponkotsunekotw/Diana-Bot) 下載安裝最新版」。\r\n" +
                "• 可自訂目標安裝資料夾，並自由決定是否安裝 Python 3.12 或沿用本機既有 Python 環境。\r\n" +
                "• 可自由選擇本地 AI 伺服端方案（安裝 Ollama / 僅設定 LM Studio / 不安裝），並透過前中後三級選單從線上動態挑選模型與查表評估速度。",
                new Font("Microsoft JhengHei UI", 9.75f, FontStyle.Regular),
                TextPrimary, 0, 2);
            custDesc.Tag = "CardLeftText";
            AddStackRow(custLeftStack, custDesc, 0);

            Button btnJumpCustom = CreateActionButton("🛠️ 進入自訂安裝 (推薦) ➡", AccentColor, Color.White, 215, 64);
            btnJumpCustom.Font = new Font("Microsoft JhengHei UI", 10.75f, FontStyle.Bold);
            btnJumpCustom.Anchor = AnchorStyles.Right;
            btnJumpCustom.Margin = new Padding(16, 8, 0, 8);
            btnJumpCustom.Click += delegate
            {
                isExpressModeSelected = false;
                ShowWizardPage(2);
            };

            custGrid.Controls.Add(custLeftStack, 0, 0);
            custGrid.Controls.Add(btnJumpCustom, 1, 0);
            cardCustom.Controls.Add(custGrid);
            AddStackRow(stack, cardCustom, 12);

            return page;
        }

        /// <summary>
        /// Builds the reusable 3-tier cascading online model selector (前選單: 官方/客製, 中選單: Google/Qwen/DeepSeek/GLM/NVIDIA, 後選單: 版本與回報尺寸)
        /// and binds it to the hardware conservative speed lookup table.
        /// </summary>
        private TableLayoutPanel BuildThreeTierModelSelectorBlock(
            out ComboBox cmbTier1,
            out ComboBox cmbTier2,
            out ComboBox cmbTier3,
            out Label lblOnlineStatus,
            out Label lblSpeedEval,
            TextBox linkedLookupTableBox)
        {
            TableLayoutPanel block = new TableLayoutPanel();
            block.Dock = DockStyle.Top;
            block.AutoSize = true;
            block.ColumnCount = 1;
            block.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));

            // Row A: 3-column header for Tier 1 (前選單), Tier 2 (中選單), and Live Fetch Button
            TableLayoutPanel tierRow12 = new TableLayoutPanel();
            tierRow12.Dock = DockStyle.Top;
            tierRow12.AutoSize = true;
            tierRow12.ColumnCount = 3;
            tierRow12.RowCount = 2;
            tierRow12.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 42f));
            tierRow12.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 58f));
            tierRow12.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

            Label lblT1 = new Label();
            lblT1.Text = "【前選單】模型來源類型 (官方 / 客製)：";
            lblT1.Font = new Font("Microsoft JhengHei UI", 9.25f, FontStyle.Bold);
            lblT1.ForeColor = TextSecondary;
            lblT1.AutoSize = true;
            lblT1.Margin = new Padding(0, 0, 8, 4);
            tierRow12.Controls.Add(lblT1, 0, 0);

            Label lblT2 = new Label();
            lblT2.Text = "【中選單】模型研發廠牌與家族系列：";
            lblT2.Font = new Font("Microsoft JhengHei UI", 9.25f, FontStyle.Bold);
            lblT2.ForeColor = TextSecondary;
            lblT2.AutoSize = true;
            lblT2.Margin = new Padding(0, 0, 8, 4);
            tierRow12.Controls.Add(lblT2, 1, 0);

            Label lblSyncHint = new Label();
            lblSyncHint.Text = "線上動態同步：";
            lblSyncHint.Font = new Font("Microsoft JhengHei UI", 9.25f, FontStyle.Bold);
            lblSyncHint.ForeColor = TextSecondary;
            lblSyncHint.AutoSize = true;
            lblSyncHint.Margin = new Padding(0, 0, 0, 4);
            tierRow12.Controls.Add(lblSyncHint, 2, 0);

            ComboBox c1 = new ComboBox();
            c1.DropDownStyle = ComboBoxStyle.DropDownList;
            c1.Dock = DockStyle.Fill;
            c1.BackColor = BgInput;
            c1.ForeColor = TextPrimary;
            c1.Font = new Font("Microsoft JhengHei UI", 9.75f, FontStyle.Bold);
            c1.Margin = new Padding(0, 0, 10, 6);
            c1.Items.AddRange(new object[]
            {
                "🏛️ 官方模型庫 (Official Library)",
                "🧪 客製與社群微調 (Custom / Community)"
            });
            c1.SelectedIndex = 0;
            tierRow12.Controls.Add(c1, 0, 1);

            ComboBox c2 = new ComboBox();
            c2.DropDownStyle = ComboBoxStyle.DropDownList;
            c2.Dock = DockStyle.Fill;
            c2.BackColor = BgInput;
            c2.ForeColor = TextPrimary;
            c2.Font = new Font("Microsoft JhengHei UI", 9.75f, FontStyle.Bold);
            c2.Margin = new Padding(0, 0, 10, 6);
            c2.Items.AddRange(new object[]
            {
                "Google (Gemma 4 / Gemma 3 系列)",
                "Qwen (阿里通義千問 3.5 / 3 / 2.5)",
                "DeepSeek (深度求索 R1 / V3 系列)",
                "GLM (智譜 ChatGLM 4 系列)",
                "NVIDIA (Nemotron 3 / Mini 系列)"
            });
            c2.SelectedIndex = 0;
            tierRow12.Controls.Add(c2, 1, 1);

            Button btnRefreshOnline = CreateActionButton("🔄 線上抓取最新列表與尺寸", BgInput, AccentColor, 200, 32);
            btnRefreshOnline.Margin = new Padding(0, 0, 0, 6);
            tierRow12.Controls.Add(btnRefreshOnline, 2, 1);

            AddStackRow(block, tierRow12, 6);

            // Row B: Tier 3 (後選單 - 版本與回報尺寸)
            TableLayoutPanel tier3HeaderRow = new TableLayoutPanel();
            tier3HeaderRow.Dock = DockStyle.Top;
            tier3HeaderRow.AutoSize = true;
            tier3HeaderRow.ColumnCount = 2;
            tier3HeaderRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 60f));
            tier3HeaderRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 40f));

            Label lblT3 = new Label();
            lblT3.Text = "【後選單】選擇模型版本與線上回報尺寸（亦可手動輸入任意模型標籤查詢）：";
            lblT3.Font = new Font("Microsoft JhengHei UI", 9.5f, FontStyle.Bold);
            lblT3.ForeColor = TextPrimary;
            lblT3.AutoSize = true;
            tier3HeaderRow.Controls.Add(lblT3, 0, 0);

            Label statusLbl = new Label();
            statusLbl.Text = "🌐 已載入模型尺寸目錄 (切換分類將自動從線上同步)";
            statusLbl.Font = new Font("Microsoft JhengHei UI", 8.75f, FontStyle.Regular);
            statusLbl.ForeColor = SuccessColor;
            statusLbl.AutoSize = true;
            statusLbl.Anchor = AnchorStyles.Right | AnchorStyles.Top;
            tier3HeaderRow.Controls.Add(statusLbl, 1, 0);

            AddStackRow(block, tier3HeaderRow, 4);

            ComboBox c3 = new ComboBox();
            c3.DropDownStyle = ComboBoxStyle.DropDown;
            c3.BackColor = BgInput;
            c3.ForeColor = TextPrimary;
            c3.Font = new Font("Consolas", 10.5f, FontStyle.Bold);
            AddStackRow(block, c3, 8);

            Label speedLbl = CreateWrappingLabel(
                hwProfile.GetSpeedEvaluationForModel("gemma4:e2b"),
                new Font("Microsoft JhengHei UI", 9.5f, FontStyle.Bold),
                SuccessColor, 0, 4);
            AddStackRow(block, speedLbl, 4);

            // Helper closures to bind Tier 1 -> Tier 2 -> Tier 3 + Online Fetch + Lookup Table
            Func<string> getCatKey = delegate()
            {
                return c1.SelectedIndex == 1 ? "custom" : "official";
            };

            Func<string> getVendorKey = delegate()
            {
                switch (c2.SelectedIndex)
                {
                    case 1: return "qwen";
                    case 2: return "deepseek";
                    case 3: return "glm";
                    case 4: return "nvidia";
                    default: return "google";
                }
            };

            Action updateSpeedAndTable = delegate()
            {
                string sel = c3.Text;
                double sizeGb = OnlineModelCatalogService.ResolveModelSizeGb(sel);
                double vFill, dSpill, dFill;
                int tierIdx;
                hwProfile.ComputeFillPercentages(sizeGb, out vFill, out dSpill, out dFill, out tierIdx);

                speedLbl.Text = hwProfile.GetSpeedEvaluationForModel(sel);
                if (tierIdx <= 2)
                {
                    speedLbl.ForeColor = SuccessColor;
                }
                else if (tierIdx <= 4)
                {
                    speedLbl.ForeColor = WarningColor;
                }
                else
                {
                    speedLbl.ForeColor = DangerColor;
                }

                if (linkedLookupTableBox != null)
                {
                    linkedLookupTableBox.Text = hwProfile.BuildModelComparisonTableText(sel);
                }
            };

            Action<bool> repopulateTier3 = delegate(bool preserveText)
            {
                string prevCleanTag = OnlineModelCatalogService.ExtractCleanModelTag(c3.Text);
                List<ModelCatalogEntry> entries = OnlineModelCatalogService.GetEntries(getCatKey(), getVendorKey());
                c3.BeginUpdate();
                c3.Items.Clear();
                int matchIdx = -1;
                for (int i = 0; i < entries.Count; i++)
                {
                    string disp = entries[i].FormatTier3Display();
                    c3.Items.Add(disp);
                    if (preserveText && string.Equals(entries[i].Tag, prevCleanTag, StringComparison.OrdinalIgnoreCase))
                    {
                        matchIdx = i;
                    }
                }
                if (matchIdx >= 0)
                {
                    c3.SelectedIndex = matchIdx;
                }
                else if (c3.Items.Count > 0)
                {
                    c3.SelectedIndex = 0;
                }
                c3.EndUpdate();
                updateSpeedAndTable();
            };

            Action<bool> triggerOnlineSync = delegate(bool force)
            {
                string cat = getCatKey();
                string ven = getVendorKey();
                string typedTag = OnlineModelCatalogService.ExtractCleanModelTag(c3.Text);
                statusLbl.Text = "⏳ 正在從 Ollama 線上庫抓取最新模型與回報尺寸...";
                statusLbl.ForeColor = WarningColor;

                Task.Run(delegate()
                {
                    int added = OnlineModelCatalogService.FetchOnlineCatalogForBucket(cat, ven, force);
                    double liveSize = -1.0;
                    if (force && !string.IsNullOrEmpty(typedTag))
                    {
                        liveSize = OnlineModelCatalogService.QueryLiveRegistryManifestSizeGb(typedTag);
                    }
                    int totalNow = OnlineModelCatalogService.GetEntries(cat, ven).Count;

                    try
                    {
                        this.BeginInvoke(new Action(delegate()
                        {
                            repopulateTier3(true);
                            if (liveSize > 0)
                            {
                                statusLbl.Text = string.Format("✓ 已從線上同步 ({0} 個版本，{1} 實測尺寸 {2:0.0} GB)", totalNow, typedTag, liveSize);
                            }
                            else
                            {
                                statusLbl.Text = string.Format("✓ 已同步線上最新模型列表 (目前分類共 {0} 個版本與尺寸)", totalNow);
                            }
                            statusLbl.ForeColor = SuccessColor;
                            updateSpeedAndTable();
                        }));
                    }
                    catch
                    {
                    }
                });
            };

            c1.SelectedIndexChanged += delegate
            {
                repopulateTier3(false);
                triggerOnlineSync(false);
            };
            c2.SelectedIndexChanged += delegate
            {
                repopulateTier3(false);
                triggerOnlineSync(false);
            };
            c3.TextChanged += delegate
            {
                updateSpeedAndTable();
            };
            btnRefreshOnline.Click += delegate
            {
                triggerOnlineSync(true);
            };

            // Initial populate
            repopulateTier3(false);

            cmbTier1 = c1;
            cmbTier2 = c2;
            cmbTier3 = c3;
            lblOnlineStatus = statusLbl;
            lblSpeedEval = speedLbl;
            return block;
        }

        /// <summary>
        /// Page 1: Express Mode Hardware VRAM/DRAM Speed Guidance & 3-Tier Dynamic Model Selection before starting Express Install.
        /// </summary>
        private Panel BuildPage1ExpressHardwareAndModel()
        {
            TableLayoutPanel stack;
            Panel page = CreateScrollablePageContainer(out stack);

            // Card 1: Hardware Diagnosis & Conservative Speed Lookup Table
            Panel hwCard = CreateCardBox(false);
            TableLayoutPanel hwStack = new TableLayoutPanel();
            hwStack.Dock = DockStyle.Top;
            hwStack.AutoSize = true;
            hwStack.ColumnCount = 1;
            hwStack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));

            AddStackRow(hwStack, CreateWrappingLabel(
                "💻 本機顯卡 VRAM (扣2GB) 與系統 DRAM (打6折) 保守速度查表診測",
                new Font("Microsoft JhengHei UI", 11f, FontStyle.Bold),
                AccentColor, 0, 6), 6);

            AddStackRow(hwStack, CreateWrappingLabel(
                hwProfile.OverallSummary,
                new Font("Microsoft JhengHei UI", 9.5f, FontStyle.Regular),
                TextPrimary, 0, 6), 6);

            txtExpressModelTable = new TextBox();
            txtExpressModelTable.Multiline = true;
            txtExpressModelTable.ReadOnly = true;
            txtExpressModelTable.BackColor = BgInput;
            txtExpressModelTable.ForeColor = TextPrimary;
            txtExpressModelTable.BorderStyle = BorderStyle.FixedSingle;
            txtExpressModelTable.Font = new Font("Consolas", 9.0f, FontStyle.Regular);
            txtExpressModelTable.Height = 195;
            txtExpressModelTable.Text = hwProfile.BuildModelComparisonTableText("gemma4:e2b");
            AddStackRow(hwStack, txtExpressModelTable, 4);

            hwCard.Controls.Add(hwStack);
            AddStackRow(stack, hwCard, 14);

            // Card 2: Mandatory .seed File & 3-Tier Dynamic Model Selector for Express Install
            Panel modCard = CreateCardBox(true);
            TableLayoutPanel modStack = new TableLayoutPanel();
            modStack.Dock = DockStyle.Top;
            modStack.AutoSize = true;
            modStack.ColumnCount = 1;
            modStack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));

            AddStackRow(modStack, CreateWrappingLabel(
                "📦 必選：指定無記憶系統種子包 (.seed 檔案，簡易安裝強制要求使用 .seed 檔)：",
                new Font("Microsoft JhengHei UI", 10.5f, FontStyle.Bold),
                WarningColor, 0, 4), 4);

            TableLayoutPanel expSeedRow = new TableLayoutPanel();
            expSeedRow.Dock = DockStyle.Top;
            expSeedRow.AutoSize = true;
            expSeedRow.ColumnCount = 2;
            expSeedRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));
            expSeedRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

            txtExpressSeedPath = new TextBox();
            txtExpressSeedPath.Dock = DockStyle.Fill;
            txtExpressSeedPath.BackColor = BgInput;
            txtExpressSeedPath.ForeColor = TextPrimary;
            txtExpressSeedPath.BorderStyle = BorderStyle.FixedSingle;
            txtExpressSeedPath.Font = new Font("Consolas", 10f);
            txtExpressSeedPath.Margin = new Padding(0, 4, 10, 4);
            txtExpressSeedPath.TextChanged += delegate
            {
                if (txtSeedPath != null && txtSeedPath.Text != txtExpressSeedPath.Text)
                {
                    txtSeedPath.Text = txtExpressSeedPath.Text;
                }
            };
            expSeedRow.Controls.Add(txtExpressSeedPath, 0, 0);

            Button btnBrowseExpSeed = CreateActionButton("📂 選擇 .seed 檔案...", BgInput, TextPrimary, 150, 32);
            btnBrowseExpSeed.Click += OnBrowseExpressSeedClick;
            expSeedRow.Controls.Add(btnBrowseExpSeed, 1, 0);
            AddStackRow(modStack, expSeedRow, 12);

            AddStackRow(modStack, CreateWrappingLabel(
                "🤖 透過前 / 中 / 後三級選單挑選預設本地模型（自動從線上抓取回報尺寸並於上方動態查表評估速度）：",
                new Font("Microsoft JhengHei UI", 10.25f, FontStyle.Bold),
                TextPrimary, 0, 6), 6);

            TableLayoutPanel expSelectorBlock = BuildThreeTierModelSelectorBlock(
                out cmbExpressTier1Category,
                out cmbExpressTier2Vendor,
                out cmbExpressModel,
                out lblExpressOnlineStatus,
                out lblExpressSpeedDynamic,
                txtExpressModelTable);
            AddStackRow(modStack, expSelectorBlock, 6);

            AddStackRow(modStack, CreateWrappingLabel(
                "📌 點擊右下角「🚀 開始簡易完整安裝」後，將自動安裝系統至 C:\\Diana-Bot、配置 Python 3.12、安裝 Ollama 與上述模型，並在完成後教您取得 Discord Bot Token！",
                new Font("Microsoft JhengHei UI", 9.25f, FontStyle.Regular),
                TextSecondary, 0, 0), 0);

            modCard.Controls.Add(modStack);
            AddStackRow(stack, modCard, 10);

            return page;
        }

        /// <summary>
        /// Page 2: Custom Step 1 - Choose Source (.seed vs. GitHub Preview Ponkotsunekotw/Diana-Bot) & Target Path.
        /// </summary>
        private Panel BuildPage2CustomPaths()
        {
            TableLayoutPanel stack;
            Panel page = CreateScrollablePageContainer(out stack);

            Panel pathCard = CreateCardBox(false);
            TableLayoutPanel pStack = new TableLayoutPanel();
            pStack.Dock = DockStyle.Top;
            pStack.AutoSize = true;
            pStack.ColumnCount = 1;
            pStack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));

            AddStackRow(pStack, CreateWrappingLabel(
                "📦 自訂步驟 1 / 3：選擇系統安裝來源（.seed 種子檔 或 GitHub 最新預覽版）與目標路徑",
                new Font("Microsoft JhengHei UI", 11f, FontStyle.Bold),
                AccentColor, 0, 8), 8);

            // Source Option 1: Local .seed file
            rbCustomSourceSeed = new RadioButton();
            rbCustomSourceSeed.Text = "來源選項 A：使用本地 .seed 無記憶系統種子包檔案（穩定封裝版）";
            rbCustomSourceSeed.Font = new Font("Microsoft JhengHei UI", 10f, FontStyle.Bold);
            rbCustomSourceSeed.ForeColor = SuccessColor;
            rbCustomSourceSeed.Checked = true;
            rbCustomSourceSeed.AutoSize = true;
            AddStackRow(pStack, rbCustomSourceSeed, 4);

            TableLayoutPanel seedRow = new TableLayoutPanel();
            seedRow.Dock = DockStyle.Top;
            seedRow.AutoSize = true;
            seedRow.ColumnCount = 2;
            seedRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));
            seedRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

            txtSeedPath = new TextBox();
            txtSeedPath.Dock = DockStyle.Fill;
            txtSeedPath.BackColor = BgInput;
            txtSeedPath.ForeColor = TextPrimary;
            txtSeedPath.BorderStyle = BorderStyle.FixedSingle;
            txtSeedPath.Font = new Font("Consolas", 10f);
            txtSeedPath.Margin = new Padding(0, 4, 10, 4);
            txtSeedPath.TextChanged += delegate
            {
                if (txtExpressSeedPath != null && txtExpressSeedPath.Text != txtSeedPath.Text)
                {
                    txtExpressSeedPath.Text = txtSeedPath.Text;
                }
            };
            seedRow.Controls.Add(txtSeedPath, 0, 0);

            Button btnBrowseSeed = CreateActionButton("📂 選擇 .seed 檔案...", BgInput, TextPrimary, 150, 32);
            btnBrowseSeed.Click += OnBrowseSeedClick;
            seedRow.Controls.Add(btnBrowseSeed, 1, 0);
            AddStackRow(pStack, seedRow, 12);

            // Source Option 2: Direct GitHub Preview (Ponkotsunekotw/Diana-Bot)
            rbCustomSourceGitHub = new RadioButton();
            rbCustomSourceGitHub.Text = "來源選項 B：🌐 預覽版 (Preview) — 直接從 GitHub 安裝最新版 (Ponkotsunekotw/Diana-Bot)";
            rbCustomSourceGitHub.Font = new Font("Microsoft JhengHei UI", 10f, FontStyle.Bold);
            rbCustomSourceGitHub.ForeColor = AccentColor;
            rbCustomSourceGitHub.Checked = false;
            rbCustomSourceGitHub.AutoSize = true;
            AddStackRow(pStack, rbCustomSourceGitHub, 4);

            AddStackRow(pStack, CreateWrappingLabel(
                "儲存庫：Ponkotsunekotw/Diana-Bot — 基於 py 與 LLM 的虛擬女兒 1.0，歡迎領養 (https://github.com/Ponkotsunekotw/Diana-Bot)",
                new Font("Microsoft JhengHei UI", 9f, FontStyle.Regular),
                TextSecondary, 0, 4), 4);

            TableLayoutPanel ghRow = new TableLayoutPanel();
            ghRow.Dock = DockStyle.Top;
            ghRow.AutoSize = true;
            ghRow.ColumnCount = 2;
            ghRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));
            ghRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

            cmbGitHubBranch = new ComboBox();
            cmbGitHubBranch.DropDownStyle = ComboBoxStyle.DropDownList;
            cmbGitHubBranch.Dock = DockStyle.Fill;
            cmbGitHubBranch.BackColor = BgInput;
            cmbGitHubBranch.ForeColor = TextPrimary;
            cmbGitHubBranch.Font = new Font("Consolas", 9.75f);
            cmbGitHubBranch.Margin = new Padding(0, 4, 10, 4);
            cmbGitHubBranch.Items.AddRange(new object[]
            {
                "main (GitHub 預設主分支 — Ponkotsunekotw/Diana-Bot)",
                "dev/gemini (GitHub 開發預覽分支)",
                "HEAD (自動追蹤儲存庫預設分支)"
            });
            cmbGitHubBranch.SelectedIndex = 0;
            ghRow.Controls.Add(cmbGitHubBranch, 0, 0);

            Button btnOpenGitHubRepo = CreateActionButton("🔗 開啟 GitHub 專案頁", BgInput, TextPrimary, 165, 32);
            btnOpenGitHubRepo.Click += delegate { Process.Start("https://github.com/Ponkotsunekotw/Diana-Bot"); };
            ghRow.Controls.Add(btnOpenGitHubRepo, 1, 0);
            AddStackRow(pStack, ghRow, 14);

            // Target Dir Row
            AddStackRow(pStack, CreateWrappingLabel(
                "目標安裝資料夾 (Target Directory)：",
                new Font("Microsoft JhengHei UI", 9.75f, FontStyle.Bold),
                TextPrimary, 0, 4), 4);

            TableLayoutPanel targetRow = new TableLayoutPanel();
            targetRow.Dock = DockStyle.Top;
            targetRow.AutoSize = true;
            targetRow.ColumnCount = 2;
            targetRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));
            targetRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

            txtTargetDir = new TextBox();
            txtTargetDir.Dock = DockStyle.Fill;
            txtTargetDir.BackColor = BgInput;
            txtTargetDir.ForeColor = TextPrimary;
            txtTargetDir.BorderStyle = BorderStyle.FixedSingle;
            txtTargetDir.Font = new Font("Consolas", 10f);
            txtTargetDir.Margin = new Padding(0, 4, 10, 4);
            targetRow.Controls.Add(txtTargetDir, 0, 0);

            Button btnBrowseTarget = CreateActionButton("📁 瀏覽目錄...", BgInput, TextPrimary, 150, 32);
            btnBrowseTarget.Click += OnBrowseTargetClick;
            targetRow.Controls.Add(btnBrowseTarget, 1, 0);
            AddStackRow(pStack, targetRow, 14);

            chkCustomSeed = new CheckBox();
            chkCustomSeed.Text = "部署系統核心檔案至目標資料夾（自動初始化純淨無記憶冷啟動保護狀態）";
            chkCustomSeed.Font = new Font("Microsoft JhengHei UI", 9.75f, FontStyle.Bold);
            chkCustomSeed.ForeColor = TextPrimary;
            chkCustomSeed.Checked = true;
            chkCustomSeed.AutoSize = true;
            AddStackRow(pStack, chkCustomSeed, 8);

            chkCustomShortcut = new CheckBox();
            chkCustomShortcut.Text = "在桌面建立「Diana Bot 智慧中控台」啟動捷徑";
            chkCustomShortcut.ForeColor = TextPrimary;
            chkCustomShortcut.Checked = true;
            chkCustomShortcut.AutoSize = true;
            AddStackRow(pStack, chkCustomShortcut, 8);

            chkCustomLaunchGui = new CheckBox();
            chkCustomLaunchGui.Text = "安裝完成後自動開啟 Diana 智慧中控台 (gui.py)";
            chkCustomLaunchGui.ForeColor = TextPrimary;
            chkCustomLaunchGui.Checked = true;
            chkCustomLaunchGui.AutoSize = true;
            AddStackRow(pStack, chkCustomLaunchGui, 4);

            pathCard.Controls.Add(pStack);
            AddStackRow(stack, pathCard, 10);

            return page;
        }

        /// <summary>
        /// Page 3: Custom Step 2 - Optional Base Python 3.12 & pip requirements.
        /// </summary>
        private Panel BuildPage3CustomPython()
        {
            TableLayoutPanel stack;
            Panel page = CreateScrollablePageContainer(out stack);

            Panel pyCard = CreateCardBox(false);
            TableLayoutPanel pyStack = new TableLayoutPanel();
            pyStack.Dock = DockStyle.Top;
            pyStack.AutoSize = true;
            pyStack.ColumnCount = 1;
            pyStack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));

            AddStackRow(pyStack, CreateWrappingLabel(
                "🐍 自訂步驟 2 / 3：基本 Python 執行環境與套件設定（可選）",
                new Font("Microsoft JhengHei UI", 11f, FontStyle.Bold),
                AccentColor, 0, 8), 8);

            lblDetectedPythonInfo = CreateWrappingLabel(
                "正在檢查本機 Python 環境...",
                new Font("Microsoft JhengHei UI", 9.75f, FontStyle.Bold),
                SuccessColor, 0, 10);
            AddStackRow(pyStack, lblDetectedPythonInfo, 10);

            chkCustomPython = new CheckBox();
            chkCustomPython.Text = "啟用 Python 環境自動配置（若未偵測到 Python 3.12 則自動下載官方 64-bit 安裝包靜默安裝）";
            chkCustomPython.Font = new Font("Microsoft JhengHei UI", 10f, FontStyle.Bold);
            chkCustomPython.ForeColor = TextPrimary;
            chkCustomPython.Checked = true;
            chkCustomPython.AutoSize = true;
            AddStackRow(pyStack, chkCustomPython, 12);

            AddStackRow(pyStack, CreateWrappingLabel(
                "選擇要安裝的 Python 版本（預設 Python 3.12）：",
                new Font("Microsoft JhengHei UI", 9.5f, FontStyle.Regular),
                TextSecondary, 0, 4), 4);

            cmbPythonVersion = new ComboBox();
            cmbPythonVersion.DropDownStyle = ComboBoxStyle.DropDownList;
            cmbPythonVersion.BackColor = BgInput;
            cmbPythonVersion.ForeColor = TextPrimary;
            cmbPythonVersion.Font = new Font("Consolas", 10f);
            cmbPythonVersion.Items.AddRange(new object[]
            {
                "3.12.10 (預設推薦 - 官方穩定版 Python 3.12 64-bit)",
                "3.12.9 (Python 3.12 64-bit)",
                "3.11.9 (Python 3.11 64-bit)"
            });
            cmbPythonVersion.SelectedIndex = 0;
            AddStackRow(pyStack, cmbPythonVersion, 12);

            chkCustomPipDeps = new CheckBox();
            chkCustomPipDeps.Text = "自動執行 pip install -r requirements.txt 安裝專案所需套件 (discord.py, chromadb, openai 等)";
            chkCustomPipDeps.ForeColor = TextPrimary;
            chkCustomPipDeps.Checked = true;
            chkCustomPipDeps.AutoSize = true;
            AddStackRow(pyStack, chkCustomPipDeps, 8);

            chkForcePython = new CheckBox();
            chkForcePython.Text = "強制重新下載並安裝 Python（即使電腦中已有 Python 也重新執行安裝程式）";
            chkForcePython.ForeColor = TextSecondary;
            chkForcePython.Checked = false;
            chkForcePython.AutoSize = true;
            AddStackRow(pyStack, chkForcePython, 4);

            pyCard.Controls.Add(pyStack);
            AddStackRow(stack, pyCard, 10);

            return page;
        }

        /// <summary>
        /// Page 4: Custom Step 3 - Optional Local AI Server (Ollama / LM Studio / None) + 3-Tier Dynamic Model Selector + Conservative Lookup Table.
        /// </summary>
        private Panel BuildPage4CustomLocalAi()
        {
            TableLayoutPanel stack;
            Panel page = CreateScrollablePageContainer(out stack);

            // Pre-create lookup table box so the 3-tier selector can bind to it immediately
            txtCustomTable = new TextBox();
            txtCustomTable.Multiline = true;
            txtCustomTable.ReadOnly = true;
            txtCustomTable.BackColor = BgInput;
            txtCustomTable.ForeColor = TextPrimary;
            txtCustomTable.BorderStyle = BorderStyle.FixedSingle;
            txtCustomTable.Font = new Font("Consolas", 9.0f);
            txtCustomTable.Height = 195;

            // Top Card: Choice of Local AI Backend & 3-Tier Dynamic Model Selector
            Panel aiCard = CreateCardBox(false);
            TableLayoutPanel aiStack = new TableLayoutPanel();
            aiStack.Dock = DockStyle.Top;
            aiStack.AutoSize = true;
            aiStack.ColumnCount = 1;
            aiStack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));

            AddStackRow(aiStack, CreateWrappingLabel(
                "🤖 自訂步驟 3 / 3：本地 AI 伺服端與模型設定（可選 Ollama / LM Studio / 不安裝）",
                new Font("Microsoft JhengHei UI", 11f, FontStyle.Bold),
                AccentColor, 0, 8), 8);

            rbLocalOllama = new RadioButton();
            rbLocalOllama.Text = "方案 A（預設）：自動下載安裝 Ollama 並拉取下方指定模型 (自動設定 Port 11434)";
            rbLocalOllama.Font = new Font("Microsoft JhengHei UI", 10f, FontStyle.Bold);
            rbLocalOllama.ForeColor = SuccessColor;
            rbLocalOllama.Checked = true;
            rbLocalOllama.AutoSize = true;
            AddStackRow(aiStack, rbLocalOllama, 6);

            rbLocalLmStudio = new RadioButton();
            rbLocalLmStudio.Text = "方案 B：我使用 LM Studio 或其他本地端點 (不下載安裝 Ollama，僅將設定檔指向 Port 1234)";
            rbLocalLmStudio.Font = new Font("Microsoft JhengHei UI", 10f, FontStyle.Bold);
            rbLocalLmStudio.ForeColor = TextPrimary;
            rbLocalLmStudio.Checked = false;
            rbLocalLmStudio.AutoSize = true;
            AddStackRow(aiStack, rbLocalLmStudio, 6);

            rbLocalNone = new RadioButton();
            rbLocalNone.Text = "方案 C：不安裝本地 AI 伺服端 (跳過此步，稍後於中控台自行設定或僅使用雲端 API)";
            rbLocalNone.Font = new Font("Microsoft JhengHei UI", 10f, FontStyle.Bold);
            rbLocalNone.ForeColor = TextSecondary;
            rbLocalNone.Checked = false;
            rbLocalNone.AutoSize = true;
            AddStackRow(aiStack, rbLocalNone, 12);

            AddStackRow(aiStack, CreateWrappingLabel(
                "透過前 / 中 / 後三級選單從線上挑選預設本地模型（或手動輸入標籤）：",
                new Font("Microsoft JhengHei UI", 9.75f, FontStyle.Bold),
                TextPrimary, 0, 4), 4);

            TableLayoutPanel custSelectorBlock = BuildThreeTierModelSelectorBlock(
                out cmbCustomTier1Category,
                out cmbCustomTier2Vendor,
                out cmbCustomModel,
                out lblCustomOnlineStatus,
                out lblCustomSpeedDynamic,
                txtCustomTable);
            AddStackRow(aiStack, custSelectorBlock, 4);

            aiCard.Controls.Add(aiStack);
            AddStackRow(stack, aiCard, 12);

            // Bottom Card: Hardware VRAM (-2GB) & DRAM (*0.6) Conservative Speed Lookup Table
            Panel speedCard = CreateCardBox(false);
            TableLayoutPanel spStack = new TableLayoutPanel();
            spStack.Dock = DockStyle.Top;
            spStack.AutoSize = true;
            spStack.ColumnCount = 1;
            spStack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));

            AddStackRow(spStack, CreateWrappingLabel(
                "📊 內建保守速度動態查表（顯存扣除 2GB / 系統 DRAM 容量打 6 折 / 假設中低端顯卡與 DDR4）：",
                new Font("Microsoft JhengHei UI", 10f, FontStyle.Bold),
                TextPrimary, 0, 6), 6);

            txtCustomTable.Text = hwProfile.BuildModelComparisonTableText(cmbCustomModel.Text);
            AddStackRow(spStack, txtCustomTable, 0);

            speedCard.Controls.Add(spStack);
            AddStackRow(stack, speedCard, 10);

            return page;
        }

        /// <summary>
        /// Page 5: Installation Progress & Live Log Console.
        /// </summary>
        private Panel BuildPage5ExecutionProgress()
        {
            Panel host = new Panel();
            host.Dock = DockStyle.Fill;
            host.BackColor = BgApp;

            TableLayoutPanel grid = new TableLayoutPanel();
            grid.Dock = DockStyle.Fill;
            grid.ColumnCount = 1;
            grid.RowCount = 4;
            grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            grid.RowStyles.Add(new RowStyle(SizeType.Percent, 100f));
            grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));

            lblInstallStatus = CreateWrappingLabel(
                "⏳ 準備開始安裝...",
                new Font("Microsoft JhengHei UI", 10.5f, FontStyle.Bold),
                WarningColor, 0, 6);
            grid.Controls.Add(lblInstallStatus, 0, 0);

            progressBar = new ProgressBar();
            progressBar.Dock = DockStyle.Top;
            progressBar.Height = 18;
            progressBar.Minimum = 0;
            progressBar.Maximum = 100;
            progressBar.Value = 0;
            progressBar.Margin = new Padding(0, 0, 0, 10);
            grid.Controls.Add(progressBar, 0, 1);

            txtLog = new RichTextBox();
            txtLog.Dock = DockStyle.Fill;
            txtLog.BackColor = Color.FromArgb(9, 13, 22);
            txtLog.ForeColor = Color.FromArgb(226, 232, 240);
            txtLog.Font = new Font("Consolas", 9.5f);
            txtLog.ReadOnly = true;
            txtLog.BorderStyle = BorderStyle.FixedSingle;
            txtLog.DetectUrls = false;
            grid.Controls.Add(txtLog, 0, 2);

            btnViewDiscordGuide = CreateActionButton(
                "📖 查看「如何取得 Discord Bot API Token 與啟用教學」並填寫金鑰 ➡",
                SuccessColor, Color.White, 360, 38);
            btnViewDiscordGuide.Margin = new Padding(0, 10, 0, 0);
            btnViewDiscordGuide.Visible = false;
            btnViewDiscordGuide.Click += delegate { ShowWizardPage(6); };
            grid.Controls.Add(btnViewDiscordGuide, 0, 3);

            host.Controls.Add(grid);
            return host;
        }

        /// <summary>
        /// Page 6: Post-Install Beginner Guide on how to obtain Discord Bot API Token & Gemini API Key,
        /// with one-click browser buttons and direct token saving!
        /// </summary>
        private Panel BuildPage6PostInstallGuide()
        {
            TableLayoutPanel stack;
            Panel page = CreateScrollablePageContainer(out stack);

            Panel guideCard = CreateCardBox(true);
            TableLayoutPanel gStack = new TableLayoutPanel();
            gStack.Dock = DockStyle.Top;
            gStack.AutoSize = true;
            gStack.ColumnCount = 1;
            gStack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));

            AddStackRow(gStack, CreateWrappingLabel(
                "🎉 安裝完成！新手必看：如何取得 Discord Bot API Token 並喚醒黛安娜",
                new Font("Microsoft JhengHei UI", 12f, FontStyle.Bold),
                SuccessColor, 0, 8), 8);

            AddStackRow(gStack, CreateWrappingLabel(
                "【步驟 1】建立您的 Discord 機器人並取得 Bot Token：\r\n" +
                "  1. 點擊下方按鈕開啟「Discord Developer Portal (開發者後台)」並登入您的 Discord 帳號。\r\n" +
                "  2. 點擊右上角藍色按鈕「New Application」，輸入名稱（例如 Diana）後建立。\r\n" +
                "  3. 點擊左側選單的「Bot」，往下捲動找到「Privileged Gateway Intents」，務必開啟以下 3 個開關：\r\n" +
                "     ✅ PRESENCE INTENT   ✅ SERVER MEMBERS INTENT   ✅ MESSAGE CONTENT INTENT (必開！)\r\n" +
                "  4. 點擊「Save Changes」儲存後，回到「Bot」頁面上方點擊「Reset Token」，複製產生的 Token 字串。\r\n\r\n" +
                "【步驟 2】將機器人邀請進您的 Discord 伺服器：\r\n" +
                "  1. 點擊左側選單「OAuth2」，在 OAuth2 URL Generator 勾選「bot」與「applications.commands」。\r\n" +
                "  2. 在下方 Bot Permissions 勾選「Administrator (管理員)」，複製最底部產生的網址貼到瀏覽器即可邀請。\r\n\r\n" +
                "【步驟 3】解除冷啟動保護、綁定專屬主人：\r\n" +
                "  • 為防止冷啟動時誤認訪客為主人，首次啟動機器人後，請在 Discord 頻道輸入指令 `/user 0000`（預設管理員密碼為 0000），黛安娜就會正式認您為專屬主人！",
                new Font("Microsoft JhengHei UI", 9.5f, FontStyle.Regular),
                TextPrimary, 0, 10), 10);

            FlowLayoutPanel linkRow = new FlowLayoutPanel();
            linkRow.Dock = DockStyle.Top;
            linkRow.AutoSize = true;
            linkRow.FlowDirection = FlowDirection.LeftToRight;
            linkRow.WrapContents = true;

            Button btnOpenDiscordDev = CreateActionButton("🌐 1. 開啟 Discord 開發者後台取得 Bot Token", AccentColor, Color.White, 310, 36);
            btnOpenDiscordDev.Margin = new Padding(0, 0, 10, 6);
            btnOpenDiscordDev.Click += delegate { Process.Start("https://discord.com/developers/applications"); };
            linkRow.Controls.Add(btnOpenDiscordDev);

            Button btnOpenAiStudio = CreateActionButton("🌐 2. 開啟 Google AI Studio 取得免費 Gemini Key (選填)", BgInput, TextPrimary, 330, 36);
            btnOpenAiStudio.Margin = new Padding(0, 0, 0, 6);
            btnOpenAiStudio.Click += delegate { Process.Start("https://aistudio.google.com/app/apikey"); };
            linkRow.Controls.Add(btnOpenAiStudio);

            AddStackRow(gStack, linkRow, 4);
            guideCard.Controls.Add(gStack);
            AddStackRow(stack, guideCard, 14);

            // Direct Quick-Save Credentials Card
            Panel tokenCard = CreateCardBox(false);
            TableLayoutPanel tStack = new TableLayoutPanel();
            tStack.Dock = DockStyle.Top;
            tStack.AutoSize = true;
            tStack.ColumnCount = 1;
            tStack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));

            AddStackRow(tStack, CreateWrappingLabel(
                "🔑 快速填寫憑證（可直接在此貼上 Token 儲存，或稍後於「Diana 智慧中控台」設定）：",
                new Font("Microsoft JhengHei UI", 10.5f, FontStyle.Bold),
                TextPrimary, 0, 6), 6);

            AddStackRow(tStack, CreateWrappingLabel(
                "Discord Bot Token (將寫入 config/discord_token.txt)：",
                new Font("Microsoft JhengHei UI", 9.5f, FontStyle.Bold),
                TextSecondary, 0, 4), 4);

            txtQuickDiscordToken = new TextBox();
            txtQuickDiscordToken.Dock = DockStyle.Top;
            txtQuickDiscordToken.BackColor = BgInput;
            txtQuickDiscordToken.ForeColor = TextPrimary;
            txtQuickDiscordToken.BorderStyle = BorderStyle.FixedSingle;
            txtQuickDiscordToken.Font = new Font("Consolas", 10f);
            AddStackRow(tStack, txtQuickDiscordToken, 10);

            AddStackRow(tStack, CreateWrappingLabel(
                "Google Gemini Chat API Key (選填，供主人高速雲端對話與離線演化使用)：",
                new Font("Microsoft JhengHei UI", 9.5f, FontStyle.Bold),
                TextSecondary, 0, 4), 4);

            txtQuickGeminiKey = new TextBox();
            txtQuickGeminiKey.Dock = DockStyle.Top;
            txtQuickGeminiKey.BackColor = BgInput;
            txtQuickGeminiKey.ForeColor = TextPrimary;
            txtQuickGeminiKey.BorderStyle = BorderStyle.FixedSingle;
            txtQuickGeminiKey.Font = new Font("Consolas", 10f);
            AddStackRow(tStack, txtQuickGeminiKey, 10);

            Button btnSaveQuickKeys = CreateActionButton("💾 儲存 Token 金鑰至安裝目錄", SuccessColor, Color.White, 240, 36);
            btnSaveQuickKeys.Click += OnSaveQuickKeysClick;
            AddStackRow(tStack, btnSaveQuickKeys, 6);

            lblQuickSaveResult = CreateWrappingLabel("", new Font("Microsoft JhengHei UI", 9.5f, FontStyle.Bold), SuccessColor, 0, 0);
            AddStackRow(tStack, lblQuickSaveResult, 0);

            tokenCard.Controls.Add(tStack);
            AddStackRow(stack, tokenCard, 10);

            return page;
        }

        // -------------------------------------------------------------------
        // Wizard State & Navigation Logic
        // -------------------------------------------------------------------

        private void InitializeDefaultValues()
        {
            string baseDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
            string defaultSeed = Path.Combine(baseDir, "Diana-Bot.seed");
            if (!File.Exists(defaultSeed))
            {
                string[] seeds = Directory.GetFiles(baseDir, "*.seed", SearchOption.TopDirectoryOnly);
                if (seeds.Length > 0)
                {
                    defaultSeed = seeds[0];
                }
            }

            txtSeedPath.Text = defaultSeed;
            if (txtExpressSeedPath != null)
            {
                txtExpressSeedPath.Text = defaultSeed;
            }
            txtTargetDir.Text = @"C:\Diana-Bot";

            string existingPy = FindInstalledPython312();
            if (!string.IsNullOrEmpty(existingPy))
            {
                lblDetectedPythonInfo.Text = "✓ 本機已偵測到可用的 Python 環境：" + existingPy;
                lblDetectedPythonInfo.ForeColor = SuccessColor;
            }
            else
            {
                lblDetectedPythonInfo.Text = "ℹ 本機尚未偵測到 Python 3.12（保持下方勾選即可自動下載並靜默安裝）。";
                lblDetectedPythonInfo.ForeColor = WarningColor;
            }
        }

        private void ShowWizardPage(int pageIndex)
        {
            currentPage = pageIndex;
            for (int i = 0; i < pages.Length; i++)
            {
                pages[i].Visible = (i == pageIndex);
            }

            if (pageIndex >= 0 && pageIndex < pages.Length && pages[pageIndex].Controls.Count > 0)
            {
                TableLayoutPanel tlp = pages[pageIndex].Controls[0] as TableLayoutPanel;
                if (tlp != null)
                {
                    ReflowScrollablePage(pages[pageIndex], tlp);
                }
            }

            switch (pageIndex)
            {
                case 0:
                    lblHeaderStepBadge.Text = "分級選單 · 請點擊右側按鈕選擇安裝模式";
                    btnBack.Visible = false;
                    btnNext.Visible = false;
                    break;

                case 1:
                    lblHeaderStepBadge.Text = "簡易完整安裝 · 線上模型選單與查表速度評估";
                    btnBack.Visible = true;
                    btnNext.Visible = true;
                    btnNext.Text = "🚀 開始簡易完整安裝";
                    btnNext.BackColor = SuccessColor;
                    break;

                case 2:
                    lblHeaderStepBadge.Text = "自訂安裝 (步驟 1 / 3) · 路徑與系統包";
                    btnBack.Visible = true;
                    btnNext.Visible = true;
                    btnNext.Text = "下一步 (Python 設定) ➡";
                    btnNext.BackColor = AccentColor;
                    break;

                case 3:
                    lblHeaderStepBadge.Text = "自訂安裝 (步驟 2 / 3) · Python 環境";
                    btnBack.Visible = true;
                    btnNext.Visible = true;
                    btnNext.Text = "下一步 (本地 AI 設定) ➡";
                    btnNext.BackColor = AccentColor;
                    break;

                case 4:
                    lblHeaderStepBadge.Text = "自訂安裝 (步驟 3 / 3) · 線上模型與保守速度查表";
                    btnBack.Visible = true;
                    btnNext.Visible = true;
                    btnNext.Text = "🚀 開始自訂安裝";
                    btnNext.BackColor = SuccessColor;
                    break;

                case 5:
                    lblHeaderStepBadge.Text = "安裝執行進度與系統日誌";
                    btnBack.Visible = !isRunningTask;
                    btnNext.Visible = !isRunningTask;
                    btnNext.Text = "📖 下一步：Discord API 教學 ➡";
                    btnNext.BackColor = AccentColor;
                    break;

                case 6:
                    lblHeaderStepBadge.Text = "完成引導 · 取得 Discord Bot API Token";
                    btnBack.Visible = true;
                    btnNext.Visible = true;
                    btnNext.Text = "🚀 啟動 Diana 中控台並結束";
                    btnNext.BackColor = SuccessColor;
                    break;
            }
        }

        private void OnBackButtonClick(object sender, EventArgs e)
        {
            if (isRunningTask)
            {
                return;
            }

            if (currentPage == 1 || currentPage == 2)
            {
                ShowWizardPage(0);
            }
            else if (currentPage == 3)
            {
                ShowWizardPage(2);
            }
            else if (currentPage == 4)
            {
                ShowWizardPage(3);
            }
            else if (currentPage == 5)
            {
                ShowWizardPage(isExpressModeSelected ? 1 : 4);
            }
            else if (currentPage == 6)
            {
                ShowWizardPage(5);
            }
        }

        private void OnNextButtonClick(object sender, EventArgs e)
        {
            if (isRunningTask)
            {
                return;
            }

            if (currentPage == 0)
            {
                ShowWizardPage(isExpressModeSelected ? 1 : 2);
            }
            else if (currentPage == 1)
            {
                // Start Express Full Install!
                isExpressModeSelected = true;
                StartInstallation(true);
            }
            else if (currentPage == 2)
            {
                ShowWizardPage(3);
            }
            else if (currentPage == 3)
            {
                ShowWizardPage(4);
            }
            else if (currentPage == 4)
            {
                // Start Custom Install!
                isExpressModeSelected = false;
                StartInstallation(false);
            }
            else if (currentPage == 5)
            {
                ShowWizardPage(6);
            }
            else if (currentPage == 6)
            {
                string targetDir = txtTargetDir.Text.Trim();
                if (Directory.Exists(targetDir) && File.Exists(Path.Combine(targetDir, "gui.py")))
                {
                    LaunchInstalledGui(targetDir, FindInstalledPython312());
                }
                this.Close();
            }
        }

        // -------------------------------------------------------------------
        // Installation Execution Pipeline
        // -------------------------------------------------------------------

        private void StartInstallation(bool isExpressMode)
        {
            string seedPath = isExpressMode ? txtExpressSeedPath.Text.Trim() : txtSeedPath.Text.Trim();
            string targetDir = isExpressMode ? @"C:\Diana-Bot" : txtTargetDir.Text.Trim();
            if (isExpressMode)
            {
                txtTargetDir.Text = targetDir;
            }

            bool doSeed = isExpressMode ? true : chkCustomSeed.Checked;
            bool useGitHubPreview = (!isExpressMode && rbCustomSourceGitHub != null && rbCustomSourceGitHub.Checked);
            string githubBranchSpec = (!isExpressMode && cmbGitHubBranch != null) ? cmbGitHubBranch.Text : "main";

            bool doPython = isExpressMode ? true : chkCustomPython.Checked;
            string pyVerSelection = isExpressMode ? "3.12.10" : cmbPythonVersion.Text;
            bool doPipDeps = isExpressMode ? true : chkCustomPipDeps.Checked;
            bool forcePython = isExpressMode ? false : chkForcePython.Checked;

            string localBackendMode = "ollama";
            if (!isExpressMode)
            {
                if (rbLocalLmStudio.Checked)
                {
                    localBackendMode = "lmstudio";
                }
                else if (rbLocalNone.Checked)
                {
                    localBackendMode = "none";
                }
            }

            string rawModelText = isExpressMode ? cmbExpressModel.Text.Trim() : cmbCustomModel.Text.Trim();
            string selectedModel = OnlineModelCatalogService.ExtractCleanModelTag(rawModelText);
            if (string.IsNullOrWhiteSpace(selectedModel))
            {
                selectedModel = "gemma4:e2b";
            }

            bool doShortcut = isExpressMode ? true : chkCustomShortcut.Checked;
            bool doLaunchGui = isExpressMode ? false : chkCustomLaunchGui.Checked;

            // Express mode strictly requires a valid .seed file!
            if (isExpressMode)
            {
                if (string.IsNullOrWhiteSpace(seedPath) ||
                    !string.Equals(Path.GetExtension(seedPath), ".seed", StringComparison.OrdinalIgnoreCase) ||
                    !File.Exists(seedPath))
                {
                    MessageBox.Show(
                        this,
                        "【簡易完整安裝】強制要求使用有效的 `.seed` 無記憶系統種子包檔案！\n\n請先選擇 `.seed` 檔案（例如 Diana-Bot.seed）。若您想從 GitHub 線上安裝最新預覽版，請點選「上一步」改用「2. 自訂安裝」。",
                        "簡易安裝強制要求 .seed 檔案",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Warning);
                    OnBrowseExpressSeedClick(this, EventArgs.Empty);
                    return;
                }
            }
            else if (doSeed && !useGitHubPreview)
            {
                if (string.IsNullOrWhiteSpace(seedPath) ||
                    !string.Equals(Path.GetExtension(seedPath), ".seed", StringComparison.OrdinalIgnoreCase) ||
                    !File.Exists(seedPath))
                {
                    MessageBox.Show(
                        this,
                        "您選擇了「使用本地 .seed 檔案」，但找不到有效的 `.seed` 檔案：\n" + seedPath + "\n\n請選擇有效的 `.seed` 檔案，或改勾選「來源選項 B：直接從 GitHub 安裝最新版」。",
                        "請選擇有效的 .seed 檔案",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Warning);
                    return;
                }
            }

            ShowWizardPage(5);
            SetUiBusy(true);
            txtLog.Clear();

            Task.Run(delegate()
            {
                RunInstallPipelineWorker(
                    isExpressMode,
                    useGitHubPreview,
                    githubBranchSpec,
                    seedPath,
                    targetDir,
                    doSeed,
                    doPython,
                    pyVerSelection,
                    doPipDeps,
                    forcePython,
                    localBackendMode,
                    selectedModel,
                    doShortcut,
                    doLaunchGui);
            });
        }

        private void RunInstallPipelineWorker(
            bool isExpressMode,
            bool useGitHubPreview,
            string githubBranchSpec,
            string seedPath,
            string targetDir,
            bool doSeed,
            bool doPython,
            string pyVerSelection,
            bool doPipDeps,
            bool forcePython,
            string localBackendMode,
            string selectedModel,
            bool doShortcut,
            bool doLaunchGui)
        {
            try
            {
                AppendLog("============================================================");
                AppendLog(isExpressMode
                    ? "⚡ 開始執行【簡易完整安裝 (Express Full Install — 強制 .seed 模式)】"
                    : (useGitHubPreview
                        ? "🌐 開始執行【自訂安裝 (GitHub 線上預覽版 — Ponkotsunekotw/Diana-Bot)】"
                        : "🛠️ 開始執行【自訂安裝 (.seed 種子包模式)】"));
                AppendLog(string.Format("💻 本機硬體：{0} (原始 {1:0.0}GB ➔ 扣2GB可用 {2:0.0}GB) / 系統DRAM：{3:0.0}GB (打6折可用 {4:0.0}GB)",
                    hwProfile.GpuName, hwProfile.VramGb, hwProfile.UsableVramGb, hwProfile.SystemDramGb, hwProfile.UsableDramGb));
                AppendLog("🤖 選定模型與保守速度查表：" + hwProfile.GetSpeedEvaluationForModel(selectedModel));
                AppendLog("============================================================");

                // Step 1: Extract .seed package OR Download from GitHub (Ponkotsunekotw/Diana-Bot)
                if (doSeed)
                {
                    if (useGitHubPreview)
                    {
                        string branchName = "main";
                        if (githubBranchSpec.StartsWith("dev/gemini"))
                        {
                            branchName = "dev/gemini";
                        }
                        else if (githubBranchSpec.StartsWith("HEAD"))
                        {
                            branchName = "HEAD";
                        }

                        string ghZipUrl = branchName == "HEAD"
                            ? "https://github.com/Ponkotsunekotw/Diana-Bot/archive/HEAD.zip"
                            : string.Format("https://github.com/Ponkotsunekotw/Diana-Bot/archive/refs/heads/{0}.zip", branchName);

                        string tempGhZip = Path.Combine(Path.GetTempPath(), "Diana-Bot-github-preview.zip");
                        SetInstallStatus("⏳ [1/4] 正在從 GitHub (Ponkotsunekotw/Diana-Bot) 下載最新預覽版...", 12, WarningColor);
                        AppendLog("🌐 正在從 GitHub 下載最新源碼包: " + ghZipUrl);
                        try
                        {
                            DownloadFileWithProgress(ghZipUrl, tempGhZip, "下載 GitHub 最新版 (" + branchName + ")", 12, 24);
                        }
                        catch (Exception ghEx)
                        {
                            AppendLog("⚠️ 指定分支下載失敗 (" + ghEx.Message + ")，自動切換至儲存庫預設分支 HEAD 重試...");
                            DownloadFileWithProgress("https://github.com/Ponkotsunekotw/Diana-Bot/archive/HEAD.zip", tempGhZip, "下載 GitHub HEAD", 12, 24);
                        }

                        SetInstallStatus("⏳ [1/4] 正在解壓縮並初始化 GitHub 預覽版系統...", 28, WarningColor);
                        AppendLog("📦 正在部署 GitHub 預覽版至: " + targetDir);
                        int ghCount = SeedPackageEngine.ExtractGitHubArchive(tempGhZip, targetDir, AppendLog);
                        AppendLog(string.Format("✅ GitHub 預覽版部署完成 (共 {0} 個檔案，已初始化無記憶純淨狀態與冷啟動保護)。", ghCount));
                    }
                    else
                    {
                        SetInstallStatus("⏳ [1/4] 正在解壓縮並初始化無記憶 .seed 系統包...", 20, WarningColor);
                        AppendLog("📦 正在部署 .seed 無記憶系統包至: " + targetDir);
                        int fileCount = SeedPackageEngine.ExtractSeedPackage(seedPath, targetDir, AppendLog);
                        AppendLog(string.Format("✅ .seed 系統包部署完成 (共 {0} 個檔案，已啟用冷啟動未綁定主人保護)。", fileCount));
                    }
                }

                // Step 2: Python 3.12 & pip requirements
                string pythonExe = FindInstalledPython312();
                if (doPython)
                {
                    SetInstallStatus("⏳ [2/4] 正在檢查並配置 Python 3.12 執行環境...", 40, WarningColor);
                    string verNumber = "3.12.10";
                    if (pyVerSelection.StartsWith("3.12.9"))
                    {
                        verNumber = "3.12.9";
                    }
                    else if (pyVerSelection.StartsWith("3.11.9"))
                    {
                        verNumber = "3.11.9";
                    }

                    if (string.IsNullOrEmpty(pythonExe) || forcePython)
                    {
                        string pyUrl = string.Format("https://www.python.org/ftp/python/{0}/python-{0}-amd64.exe", verNumber);
                        string tempPyInstaller = Path.Combine(Path.GetTempPath(), string.Format("python-{0}-amd64.exe", verNumber));
                        AppendLog("⬇️ 正在下載官方 Python 安裝程式: " + pyUrl);
                        DownloadFileWithProgress(pyUrl, tempPyInstaller, "下載 Python " + verNumber, 40, 55);

                        AppendLog("⚙️ 正在靜默安裝 Python " + verNumber + "...");
                        SetInstallStatus("⏳ [2/4] 正在靜默安裝 Python " + verNumber + " (約需 1~2 分鐘)...", 56, WarningColor);
                        RunProcessAndStream(
                            tempPyInstaller,
                            "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_pip=1 Include_tcltk=1 Include_launcher=1",
                            Path.GetTempPath());
                        pythonExe = FindInstalledPython312();
                    }
                    else
                    {
                        AppendLog("✅ 已偵測到現有 Python 環境: " + pythonExe);
                    }

                    if (doPipDeps)
                    {
                        string reqFile = Path.Combine(targetDir, "requirements.txt");
                        if (!string.IsNullOrEmpty(pythonExe) && File.Exists(reqFile))
                        {
                            SetInstallStatus("⏳ [2/4] 正在安裝 requirements.txt 依賴套件...", 62, WarningColor);
                            AppendLog("📦 正在執行 pip install -r requirements.txt ...");
                            RunProcessAndStream(pythonExe, "-m pip install --upgrade pip", targetDir);
                            int pipExit = RunProcessAndStream(pythonExe, "-m pip install -r \"" + reqFile + "\"", targetDir);
                            if (pipExit == 0)
                            {
                                AppendLog("✅ Python 依賴套件安裝完成！");
                            }
                            else
                            {
                                AppendLog(string.Format("⚠️ pip 安裝回傳碼 {0}，稍後可於中控台點擊「安裝依賴」重試。", pipExit));
                            }
                        }
                    }
                }

                // Step 3: Local AI Backend (Ollama / LM Studio / Skip)
                if (localBackendMode == "ollama")
                {
                    SetInstallStatus("⏳ [3/4] 正在檢查並安裝 Ollama 本地 AI 伺服端...", 74, WarningColor);
                    string ollamaExe = FindInstalledOllama();
                    if (string.IsNullOrEmpty(ollamaExe))
                    {
                        string ollamaUrl = "https://ollama.com/download/OllamaSetup.exe";
                        string tempOllamaInstaller = Path.Combine(Path.GetTempPath(), "OllamaSetup.exe");
                        AppendLog("⬇️ 正在下載官方 Ollama 安裝程式: " + ollamaUrl);
                        DownloadFileWithProgress(ollamaUrl, tempOllamaInstaller, "下載 OllamaSetup.exe", 74, 84);

                        AppendLog("⚙️ 正在靜默安裝 Ollama (OllamaSetup.exe /SILENT)...");
                        SetInstallStatus("⏳ [3/4] 正在靜默安裝 Ollama 伺服端...", 85, WarningColor);
                        RunProcessAndStream(tempOllamaInstaller, "/SILENT", Path.GetTempPath());
                        ollamaExe = FindInstalledOllama();
                    }
                    else
                    {
                        AppendLog("✅ 已偵測到現有 Ollama: " + ollamaExe);
                    }

                    string primaryTag = selectedModel;
                    if (string.Equals(primaryTag, "gemma4-e2b", StringComparison.OrdinalIgnoreCase))
                    {
                        primaryTag = "gemma4:e2b";
                    }

                    if (!string.IsNullOrEmpty(ollamaExe))
                    {
                        EnsureOllamaServerRunning(ollamaExe);
                        SetInstallStatus("⏳ [3/4] 正在透過 Ollama 拉取模型 " + primaryTag + " ...", 90, WarningColor);
                        AppendLog("🤖 正在執行: ollama pull " + primaryTag);
                        int pullExit = RunProcessAndStream(ollamaExe, "pull " + primaryTag, targetDir);
                        if (pullExit == 0)
                        {
                            AppendLog("✅ 本地模型拉取完成: " + primaryTag);
                        }
                        else
                        {
                            AppendLog("⚠️ 模型拉取未完全成功，已先寫入設定檔，稍後可執行 `ollama pull " + primaryTag + "`。");
                        }
                    }

                    if (Directory.Exists(targetDir))
                    {
                        SeedPackageEngine.UpdateGuiConfigForLocalAI(targetDir, "http://127.0.0.1:11434/v1", primaryTag, AppendLog);
                    }
                }
                else if (localBackendMode == "lmstudio")
                {
                    AppendLog("🖥️ 已選擇使用 LM Studio 或既有本地端點 (跳過安裝 Ollama)。");
                    if (Directory.Exists(targetDir))
                    {
                        SeedPackageEngine.UpdateGuiConfigForLocalAI(targetDir, "http://127.0.0.1:1234/v1", selectedModel, AppendLog);
                    }
                }
                else
                {
                    AppendLog("☁️ 已選擇跳過本地 AI 伺服端安裝。");
                }

                // Step 4: Desktop Shortcut
                if (doShortcut && Directory.Exists(targetDir))
                {
                    SetInstallStatus("⏳ [4/4] 正在建立桌面捷徑...", 96, WarningColor);
                    CreateDesktopShortcut(targetDir);
                }

                SetInstallStatus("✓ 安裝全部完成！請點選下方按鈕或「下一步」查看 Discord Bot API Token 取得教學。", 100, SuccessColor);
                AppendLog("============================================================");
                AppendLog("🎉 Diana-Bot 安裝完成！目標目錄: " + targetDir);
                AppendLog("👉 下一步：取得 Discord Bot Token 並貼入設定，即可啟動黛安娜！");
                AppendLog("============================================================");

                this.BeginInvoke(new Action(delegate()
                {
                    btnViewDiscordGuide.Visible = true;
                    if (isExpressMode)
                    {
                        // Automatically advance Express mode users to the Discord Bot API Guide page!
                        ShowWizardPage(6);
                    }
                    else if (doLaunchGui && File.Exists(Path.Combine(targetDir, "gui.py")))
                    {
                        LaunchInstalledGui(targetDir, pythonExe);
                    }
                }));
            }
            catch (Exception ex)
            {
                AppendLog("❌ 安裝過程中發生錯誤: " + ex.Message);
                SetInstallStatus("✗ 安裝中斷: " + ex.Message, 0, DangerColor);
            }
            finally
            {
                SetUiBusy(false);
            }
        }

        private void OnSaveQuickKeysClick(object sender, EventArgs e)
        {
            try
            {
                string targetDir = txtTargetDir.Text.Trim();
                string configDir = Path.Combine(targetDir, "config");
                Directory.CreateDirectory(configDir);
                UTF8Encoding utf8NoBom = new UTF8Encoding(false);

                string discordToken = txtQuickDiscordToken.Text.Trim();
                string geminiKey = txtQuickGeminiKey.Text.Trim();

                if (string.IsNullOrEmpty(discordToken) && string.IsNullOrEmpty(geminiKey))
                {
                    MessageBox.Show(this, "請先貼上 Discord Bot Token 或 Gemini API Key 再點擊儲存！", "提示", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return;
                }

                if (!string.IsNullOrEmpty(discordToken))
                {
                    File.WriteAllText(Path.Combine(configDir, "discord_token.txt"), discordToken + "\r\n", utf8NoBom);
                }
                if (!string.IsNullOrEmpty(geminiKey))
                {
                    File.WriteAllText(Path.Combine(configDir, "google_api_key_chat.txt"), geminiKey + "\r\n", utf8NoBom);
                    File.WriteAllText(Path.Combine(configDir, "google_api_key_evolution.txt"), geminiKey + "\r\ngemini-3.5-flash-lite\r\n", utf8NoBom);
                    File.WriteAllText(Path.Combine(configDir, "google_api_key_image.txt"), geminiKey + "\r\n", utf8NoBom);
                }

                lblQuickSaveResult.Text = "✓ 已成功將憑證寫入 " + configDir + "！現在可點擊右下角「🚀 啟動 Diana 中控台並結束」。";
                lblQuickSaveResult.ForeColor = SuccessColor;
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, "寫入金鑰失敗：" + ex.Message, "錯誤", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        // -------------------------------------------------------------------
        // Helpers (Logging, File Dialogs, Process Runner, Shortcuts)
        // -------------------------------------------------------------------

        private void AppendLog(string message)
        {
            if (this.InvokeRequired)
            {
                this.BeginInvoke(new Action<string>(AppendLog), message);
                return;
            }
            string timestamp = DateTime.Now.ToString("HH:mm:ss");
            txtLog.AppendText(string.Format("[{0}] {1}{2}", timestamp, message, Environment.NewLine));
            txtLog.ScrollToCaret();
        }

        private void SetInstallStatus(string text, int pct, Color color)
        {
            if (this.InvokeRequired)
            {
                this.BeginInvoke(new Action<string, int, Color>(SetInstallStatus), text, pct, color);
                return;
            }
            lblInstallStatus.Text = text;
            lblInstallStatus.ForeColor = color;
            if (pct >= 0)
            {
                progressBar.Value = Math.Max(0, Math.Min(100, pct));
            }
        }

        private void SetUiBusy(bool busy)
        {
            if (this.InvokeRequired)
            {
                this.BeginInvoke(new Action<bool>(SetUiBusy), busy);
                return;
            }
            isRunningTask = busy;
            btnBack.Enabled = !busy;
            btnNext.Enabled = !busy;
            btnPackSeedFooter.Enabled = !busy;
            if (!busy && currentPage == 5)
            {
                btnBack.Visible = true;
                btnNext.Visible = true;
            }
        }

        private void OnBrowseExpressSeedClick(object sender, EventArgs e)
        {
            using (OpenFileDialog ofd = new OpenFileDialog())
            {
                ofd.Title = "選擇 Diana-Bot 無記憶系統種子包 (.seed 檔案)";
                ofd.Filter = "Diana Seed 無記憶系統種子包 (*.seed)|*.seed";
                if (File.Exists(txtExpressSeedPath.Text))
                {
                    ofd.InitialDirectory = Path.GetDirectoryName(txtExpressSeedPath.Text);
                }
                if (ofd.ShowDialog(this) == DialogResult.OK)
                {
                    txtExpressSeedPath.Text = ofd.FileName;
                    txtSeedPath.Text = ofd.FileName;
                }
            }
        }

        private void OnBrowseSeedClick(object sender, EventArgs e)
        {
            using (OpenFileDialog ofd = new OpenFileDialog())
            {
                ofd.Title = "選擇 Diana-Bot 無記憶系統種子包 (.seed)";
                ofd.Filter = "Diana Seed 無記憶系統種子包 (*.seed)|*.seed";
                if (File.Exists(txtSeedPath.Text))
                {
                    ofd.InitialDirectory = Path.GetDirectoryName(txtSeedPath.Text);
                }
                if (ofd.ShowDialog(this) == DialogResult.OK)
                {
                    txtSeedPath.Text = ofd.FileName;
                    if (txtExpressSeedPath != null)
                    {
                        txtExpressSeedPath.Text = ofd.FileName;
                    }
                    if (rbCustomSourceSeed != null)
                    {
                        rbCustomSourceSeed.Checked = true;
                    }
                }
            }
        }

        private void OnBrowseTargetClick(object sender, EventArgs e)
        {
            using (FolderBrowserDialog fbd = new FolderBrowserDialog())
            {
                fbd.Description = "選擇 Diana-Bot 目標安裝資料夾";
                fbd.SelectedPath = txtTargetDir.Text;
                if (fbd.ShowDialog(this) == DialogResult.OK)
                {
                    txtTargetDir.Text = fbd.SelectedPath;
                }
            }
        }

        private void OnPackSeedClick(object sender, EventArgs e)
        {
            if (isRunningTask)
            {
                return;
            }

            string baseDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
            string sourceDir = baseDir;
            if (!File.Exists(Path.Combine(sourceDir, "bot.py")))
            {
                using (FolderBrowserDialog fbd = new FolderBrowserDialog())
                {
                    fbd.Description = "請選擇要封裝為無記憶 .seed 的 Diana-Bot 專案來源資料夾 (需包含 bot.py)";
                    fbd.SelectedPath = baseDir;
                    if (fbd.ShowDialog(this) != DialogResult.OK)
                    {
                        return;
                    }
                    sourceDir = fbd.SelectedPath;
                }
            }

            using (SaveFileDialog sfd = new SaveFileDialog())
            {
                sfd.Title = "儲存無記憶系統種子包 (.seed)";
                sfd.Filter = "Diana Seed 無記憶系統包 (*.seed)|*.seed";
                sfd.FileName = "Diana-Bot.seed";
                sfd.InitialDirectory = sourceDir;
                if (sfd.ShowDialog(this) != DialogResult.OK)
                {
                    return;
                }

                string outSeed = sfd.FileName;
                ShowWizardPage(5);
                SetUiBusy(true);
                txtLog.Clear();
                SetInstallStatus("⏳ 正在封裝無記憶 .seed 系統包...", 25, WarningColor);

                Task.Run(delegate()
                {
                    try
                    {
                        AppendLog("=== 開始封裝無記憶 .seed 系統包 ===");
                        int count = SeedPackageEngine.CreateCleanSeedPackage(sourceDir, outSeed, AppendLog);
                        FileInfo fi = new FileInfo(outSeed);
                        AppendLog(string.Format("✅ 封裝完成！共寫入 {0} 個檔案 ({1:0.00} MB) -> {2}", count, fi.Length / 1048576.0, outSeed));
                        this.BeginInvoke(new Action(delegate()
                        {
                            txtSeedPath.Text = outSeed;
                        }));
                        SetInstallStatus("✓ 無記憶 .seed 系統包封裝完成！", 100, SuccessColor);
                    }
                    catch (Exception ex)
                    {
                        AppendLog("❌ 封裝失敗: " + ex.Message);
                        SetInstallStatus("✗ 封裝失敗: " + ex.Message, 0, DangerColor);
                    }
                    finally
                    {
                        SetUiBusy(false);
                    }
                });
            }
        }

        private void DownloadFileWithProgress(string url, string destinationPath, string stageLabel, int startProgress, int endProgress)
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
            req.UserAgent = "DianaSeedInstaller/2.0 (Windows NT 10.0; Win64; x64)";
            req.Timeout = 30000;
            req.ReadWriteTimeout = 30000;

            using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
            using (Stream respStream = resp.GetResponseStream())
            using (FileStream outStream = new FileStream(destinationPath, FileMode.Create, FileAccess.Write, FileShare.None))
            {
                long totalBytes = resp.ContentLength;
                byte[] buffer = new byte[65536];
                long received = 0;
                int read;
                int lastReportedPct = -1;

                while ((read = respStream.Read(buffer, 0, buffer.Length)) > 0)
                {
                    outStream.Write(buffer, 0, read);
                    received += read;
                    if (totalBytes > 0)
                    {
                        int dlPct = (int)((received * 100L) / totalBytes);
                        if (dlPct != lastReportedPct && dlPct % 5 == 0)
                        {
                            lastReportedPct = dlPct;
                            int mappedProgress = startProgress + (int)((endProgress - startProgress) * (dlPct / 100.0));
                            double mbDone = received / 1048576.0;
                            double mbTotal = totalBytes / 1048576.0;
                            SetInstallStatus(
                                string.Format("⏳ {0}: {1:0.0} MB / {2:0.0} MB ({3}%)", stageLabel, mbDone, mbTotal, dlPct),
                                mappedProgress,
                                WarningColor);
                        }
                    }
                }
            }
        }

        private int RunProcessAndStream(string fileName, string arguments, string workingDir)
        {
            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = fileName;
            psi.Arguments = arguments;
            psi.WorkingDirectory = Directory.Exists(workingDir) ? workingDir : AppDomain.CurrentDomain.BaseDirectory;
            psi.UseShellExecute = false;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.CreateNoWindow = true;
            psi.StandardOutputEncoding = Encoding.UTF8;
            psi.StandardErrorEncoding = Encoding.UTF8;

            using (Process proc = new Process())
            {
                proc.StartInfo = psi;
                proc.OutputDataReceived += delegate(object s, DataReceivedEventArgs e)
                {
                    if (!string.IsNullOrWhiteSpace(e.Data))
                    {
                        AppendLog("  " + e.Data);
                    }
                };
                proc.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e)
                {
                    if (!string.IsNullOrWhiteSpace(e.Data))
                    {
                        AppendLog("  " + e.Data);
                    }
                };

                proc.Start();
                proc.BeginOutputReadLine();
                proc.BeginErrorReadLine();
                proc.WaitForExit();
                return proc.ExitCode;
            }
        }

        private string FindInstalledPython312()
        {
            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string progFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
            string[] candidates = new string[]
            {
                Path.Combine(localAppData, "Programs", "Python", "Python312", "python.exe"),
                Path.Combine(progFiles, "Python312", "python.exe"),
                @"C:\Python312\python.exe",
                Path.Combine(localAppData, "Programs", "Python", "Python311", "python.exe")
            };

            foreach (string c in candidates)
            {
                if (File.Exists(c))
                {
                    return c;
                }
            }

            try
            {
                ProcessStartInfo psi = new ProcessStartInfo("py.exe", "-3.12 -c \"import sys; print(sys.executable)\"");
                psi.UseShellExecute = false;
                psi.RedirectStandardOutput = true;
                psi.CreateNoWindow = true;
                using (Process p = Process.Start(psi))
                {
                    string output = p.StandardOutput.ReadToEnd().Trim();
                    p.WaitForExit(3000);
                    if (p.ExitCode == 0 && !string.IsNullOrEmpty(output) && File.Exists(output))
                    {
                        return output;
                    }
                }
            }
            catch
            {
            }

            return null;
        }

        private string FindInstalledOllama()
        {
            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string progFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
            string[] candidates = new string[]
            {
                Path.Combine(localAppData, "Programs", "Ollama", "ollama.exe"),
                Path.Combine(progFiles, "Ollama", "ollama.exe")
            };

            foreach (string c in candidates)
            {
                if (File.Exists(c))
                {
                    return c;
                }
            }

            try
            {
                ProcessStartInfo psi = new ProcessStartInfo("where.exe", "ollama");
                psi.UseShellExecute = false;
                psi.RedirectStandardOutput = true;
                psi.CreateNoWindow = true;
                using (Process p = Process.Start(psi))
                {
                    string output = p.StandardOutput.ReadToEnd();
                    p.WaitForExit(3000);
                    if (p.ExitCode == 0 && !string.IsNullOrWhiteSpace(output))
                    {
                        string[] lines = output.Split(new char[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);
                        if (lines.Length > 0 && File.Exists(lines[0].Trim()))
                        {
                            return lines[0].Trim();
                        }
                    }
                }
            }
            catch
            {
            }

            return null;
        }

        private void EnsureOllamaServerRunning(string ollamaExe)
        {
            if (IsOllamaEndpointAlive())
            {
                AppendLog("• Ollama 本地伺服端已在背景運行中 (http://127.0.0.1:11434)。");
                return;
            }

            AppendLog("• 正在啟動 Ollama 背景服務 (ollama serve)...");
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo(ollamaExe, "serve");
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                Process.Start(psi);
            }
            catch (Exception ex)
            {
                AppendLog("⚠️ 啟動 ollama serve 時發生例外: " + ex.Message);
            }

            for (int i = 0; i < 12; i++)
            {
                Thread.Sleep(1000);
                if (IsOllamaEndpointAlive())
                {
                    AppendLog("✓ Ollama 伺服端已就緒 (127.0.0.1:11434)。");
                    return;
                }
            }
        }

        private bool IsOllamaEndpointAlive()
        {
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:11434/api/tags");
                req.Timeout = 1500;
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                {
                    return resp.StatusCode == HttpStatusCode.OK;
                }
            }
            catch
            {
                return false;
            }
        }

        private void CreateDesktopShortcut(string targetDir)
        {
            try
            {
                string desktopDir = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
                string shortcutPath = Path.Combine(desktopDir, "Diana Bot 智慧中控台.lnk");
                string targetBat = Path.Combine(targetDir, "start_gui.bat");

                Type shellType = Type.GetTypeFromProgID("WScript.Shell");
                if (shellType != null)
                {
                    object shell = Activator.CreateInstance(shellType);
                    object shortcut = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { shortcutPath });
                    Type scType = shortcut.GetType();
                    scType.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { targetBat });
                    scType.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut, new object[] { targetDir });
                    scType.InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { "啟動 Diana-Bot 智慧自適應中控台" });
                    scType.InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
                    AppendLog("✅ 已建立桌面捷徑: " + shortcutPath);
                }
            }
            catch (Exception ex)
            {
                AppendLog("⚠️ 建立桌面捷徑失敗: " + ex.Message);
            }
        }

        private void LaunchInstalledGui(string targetDir, string pythonExe)
        {
            try
            {
                string guiPy = Path.Combine(targetDir, "gui.py");
                if (!string.IsNullOrEmpty(pythonExe) && File.Exists(pythonExe))
                {
                    string pythonw = Path.Combine(Path.GetDirectoryName(pythonExe), "pythonw.exe");
                    string runner = File.Exists(pythonw) ? pythonw : pythonExe;
                    ProcessStartInfo psi = new ProcessStartInfo(runner, "\"" + guiPy + "\"");
                    psi.WorkingDirectory = targetDir;
                    psi.UseShellExecute = false;
                    Process.Start(psi);
                }
                else
                {
                    ProcessStartInfo psi = new ProcessStartInfo(Path.Combine(targetDir, "start_gui.bat"));
                    psi.WorkingDirectory = targetDir;
                    psi.UseShellExecute = true;
                    Process.Start(psi);
                }
            }
            catch (Exception ex)
            {
                AppendLog("⚠️ 自動啟動中控台失敗: " + ex.Message);
            }
        }
    }
}
