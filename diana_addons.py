"""
diana_addons.py
===============
黛安娜 (Diana) 擴充附帶功能模組 (Add-ons & Evolution Sandbox)

【設計目的：安全隔離主程式】
- `bot.py` 僅負責「核心對話引擎、冷啟動與主人身分防護、記憶 (RAG)、連線與系統管理」，
  在日常自動演化 (diana_evolution.py) 中受到絕對保護，不會被覆寫。
- 所有「附帶生活工具與娛樂小功能」（例如：定時提醒/鬧鐘、日曆行程辨識、美食轉盤抽籤、
  以及未來透過 `/upgrade` 許願池或凌晨自動演化新增的各種小遊戲/小工具），全部集中在此模組。
- 就算演化引擎生成的擴充程式碼發生未預期問題，`bot.py` 主系統也有 `try-except` 保護，
  確保黛安娜的核心聊天與系統管理永遠不會崩潰。

【模組介面契約 (Contract) — 演化引擎修改此檔案時必須保留以下 3 個入口函式簽名】
1. `setup_addons(bot, ctx: dict)`：
   在啟動時由 `bot.py` 呼叫，負責註冊附帶的斜線指令 (`@bot.tree.command`)。
2. `check_scheduled_addons(bot, now: datetime, state: dict, ctx: dict)` (async)：
   由 `bot.py` 的背景每分鐘定時任務呼叫，處理定時提醒、鬧鐘或日曆通知。
3. `handle_message_addons(message, clean_prompt: str, is_master: bool, now: datetime, ctx: dict)` (async)：
   由 `bot.py` 在收到訊息並回覆後呼叫，處理自然語言時間/行程提醒偵測或關鍵字小彩蛋。
"""

import os
import re
import random
import discord
from discord import app_commands
from datetime import datetime, timedelta


# ---------------------------------------------------------
# 1. 自然語言時間、日曆行程與鬧鐘提醒解析
# ---------------------------------------------------------
TIME_MENTION_PATTERN = re.compile(
    r'(?P<day>今天|明天|後天|大後天|下週[一二三四五六日天]|下禮拜[一二三四五六日天])?\s*'
    r'(?P<period>清晨|凌晨|早上|上午|中午|下午|晚上|傍晚)?\s*'
    r'(?:(?P<hour>\d{1,2})\s*(?:[:：]\s*(?P<minute>\d{2})|點\s*(?P<half>半)?))?'
)
PLAN_KEYWORDS = ['要', '得', '需要', '會去', '去', '有空', '有事', '約', '提醒', '鬧鐘', '開會', '出門']


def parse_reminder_time(raw: str, now: datetime):
    """解析提醒時間字串 (支援 ISO 格式或 MM-DD HH:MM / MM/DD HH:MM)。"""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    try:
        raw_normalized = raw.replace("/", "-")
        return datetime.strptime(raw_normalized, "%m-%d %H:%M").replace(year=now.year)
    except ValueError:
        return None


def detect_time_mention(text: str, now: datetime):
    """從主人的日常對話中偵測是否有提到未來的行程或時間點，回傳建議提醒的 datetime 清單。"""
    if not text or not any(kw in text for kw in PLAN_KEYWORDS):
        return None

    for match in TIME_MENTION_PATTERN.finditer(text):
        day_str = match.group('day')
        period_str = match.group('period')
        hour_str = match.group('hour')

        if not day_str and not hour_str and not period_str:
            continue

        target_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if day_str:
            if day_str == '明天':
                target_date += timedelta(days=1)
            elif day_str == '後天':
                target_date += timedelta(days=2)
            elif day_str == '大後天':
                target_date += timedelta(days=3)
            elif day_str.startswith('下週') or day_str.startswith('下禮拜'):
                week_map = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}
                target_weekday = week_map[day_str[-1]]
                days_ahead = 7 - now.weekday() + target_weekday
                target_date += timedelta(days=days_ahead)

        remind_times = []

        if hour_str:
            hour = int(hour_str)
            minute = int(match.group('minute')) if match.group('minute') else (30 if match.group('half') else 0)
            if hour > 23 or minute > 59:
                continue
            if period_str in ('下午', '晚上', '傍晚') and hour < 12:
                hour += 12
            elif period_str in ('凌晨', '清晨') and hour == 12:
                hour = 0

            target_time = target_date.replace(hour=hour, minute=minute)
            if not day_str and target_time <= now:
                target_time += timedelta(days=1)

            remind_at = target_time - timedelta(minutes=30)
            remind_times.append(remind_at if remind_at > now else target_time)

        elif period_str:
            if period_str in ('下午',):
                hour = 14
            elif period_str in ('晚上', '傍晚'):
                hour = 19
            elif period_str in ('中午',):
                hour = 12
            else:
                hour = 9

            target_time = target_date.replace(hour=hour, minute=0)
            if not day_str and target_time <= now:
                target_time += timedelta(days=1)

            remind_at = target_time - timedelta(minutes=30)
            remind_times.append(remind_at if remind_at > now else target_time)

        else:
            night_before = target_date - timedelta(days=1) + timedelta(hours=22)
            morning_of = target_date + timedelta(hours=7, minutes=30)

            if night_before > now:
                remind_times.append(night_before)
            if morning_of > now:
                remind_times.append(morning_of)

        if remind_times:
            return remind_times

    return None


class ReminderConfirmView(discord.ui.View):
    """詢問主人是否要將行程加入提醒的互動按鈕。"""
    def __init__(self, channel_id: str, remind_times: list, content: str, ctx: dict):
        super().__init__(timeout=120)
        self.channel_id = channel_id
        self.remind_times = remind_times
        self.content = content
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        get_master_id = self.ctx["get_master_id"]
        master_id = get_master_id()
        if not master_id or str(interaction.user.id) != str(master_id):
            await interaction.response.send_message("❌ 只有主人可以設定這個提醒喔！", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="要！", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        reminder_file = self.ctx["REMINDER_FILE"]
        get_file_lock = self.ctx["get_file_lock"]
        load_json_async = self.ctx["load_json_async"]
        save_json_async = self.ctx["save_json_async"]

        async with get_file_lock(reminder_file):
            reminders = await load_json_async(reminder_file, [])
            for r_time in self.remind_times:
                reminders.append({
                    "channel_id": self.channel_id,
                    "time": r_time.isoformat(),
                    "content": self.content
                })
            await save_json_async(reminder_file, reminders)

        for child in self.children:
            child.disabled = True

        time_strs = " 與 ".join([f"`{t.strftime('%m-%d %H:%M')}`" for t in self.remind_times])
        await interaction.response.edit_message(
            content=f"✅ 黛安娜記下來了！會在 {time_strs} 提醒主人喔！",
            view=self
        )
        self.stop()

    @discord.ui.button(label="不用啦", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="好啦，那黛安娜就不提醒主人了！", view=self)
        self.stop()


# ---------------------------------------------------------
# 2. 定時背景檢查入口 (提醒、鬧鐘、日曆行程)
# ---------------------------------------------------------
async def check_scheduled_addons(bot, now: datetime, state: dict, ctx: dict):
    """每分鐘由 bot.py 背景任務呼叫，檢查是否有到期的提醒或鬧鐘。"""
    reminder_file = ctx["REMINDER_FILE"]
    get_file_lock = ctx["get_file_lock"]
    load_json_async = ctx["load_json_async"]
    save_json_async = ctx["save_json_async"]
    is_channel_ignored = ctx["is_channel_ignored"]
    send_action_message = ctx["send_action_message"]

    async with get_file_lock(reminder_file):
        reminders = await load_json_async(reminder_file, [])
        remaining = []
        for item in reminders:
            target_time = parse_reminder_time(item.get("time", ""), now)
            if target_time is None:
                remaining.append(item)
                continue
            if now >= target_time:
                ch_id = str(item["channel_id"])
                if is_channel_ignored(ch_id, state):
                    print(f"🔕 [提醒略過] 頻道 {ch_id} 在忽略清單中，提醒內容捨棄不發送。")
                else:
                    channel = bot.get_channel(int(ch_id))
                    if channel:
                        await send_action_message(
                            channel,
                            f"⏰ *(拉拉主人的袖子)* 主人！黛安娜提醒你：『{item['content']}』的時間到了喔！"
                        )
            else:
                remaining.append(item)
        if len(reminders) != len(remaining):
            await save_json_async(reminder_file, remaining)


# ---------------------------------------------------------
# 3. 訊息觸發之附帶功能入口 (行程提醒詢問、互動彩蛋)
# ---------------------------------------------------------
async def handle_message_addons(message, clean_prompt: str, is_master: bool, now: datetime, ctx: dict):
    """在主對話回覆完成後呼叫，僅對主人觸發行程提醒偵測與擴充小彩蛋。"""
    if not is_master or not clean_prompt:
        return

    channel_id = str(message.channel.id)
    remind_times = detect_time_mention(clean_prompt, now)
    if remind_times:
        view = ReminderConfirmView(
            channel_id=channel_id,
            remind_times=remind_times,
            content=clean_prompt[:80],
            ctx=ctx
        )
        if len(remind_times) >= 2:
            msg_text = (
                f"對了主人，既然沒有指定具體時間，黛安娜要不要在 "
                f"`{remind_times[0].strftime('%m-%d %H:%M')}`(前晚睡前) 和 "
                f"`{remind_times[1].strftime('%m-%d %H:%M')}`(當天早上) 提醒你這件事呀？"
            )
        else:
            msg_text = (
                f"對了主人，黛安娜要不要在 `{remind_times[0].strftime('%m-%d %H:%M')}`"
                f"（提前 30 分鐘）提醒你這件事呀？"
            )
        await message.channel.send(msg_text, view=view)


# ---------------------------------------------------------
# 4. 斜線指令註冊入口 (提醒、日曆/待辦清單、餐點轉盤、自訂轉盤等)
# ---------------------------------------------------------
def get_addons_help_lines() -> list[str]:
    """提供附帶功能模組的指令說明，供 /help 動態彙整顯示。"""
    return [
        "* `/what_to_eat [分類]`：不知道吃什麼嗎？讓黛安娜為主人抽籤推薦餐點！",
        "* `/wheel <選項清單>`：自訂幸運轉盤（用空白或逗號分隔選項），讓黛安娜幫主人轉轉盤做決定！",
        "* `/remind <時間> <內容>`：設定定時提醒或鬧鐘。",
        "* `/reminders [刪除編號]`：查看目前的待辦提醒日曆清單，或刪除指定編號的提醒。",
        "* `/upgrade <功能描述>`：紀錄你希望黛安娜未來新增的附帶小功能或修改建議。",
    ]


def setup_addons(bot, ctx: dict):
    """註冊所有附帶功能的 Slash Commands。"""
    temp_reply = ctx["temp_reply"]
    send_action_interaction = ctx["send_action_interaction"]
    get_file_lock = ctx["get_file_lock"]
    load_json_async = ctx["load_json_async"]
    save_json_async = ctx["save_json_async"]
    REMINDER_FILE = ctx["REMINDER_FILE"]
    UPGRADE_FILE = ctx["UPGRADE_FILE"]
    UPDATE_DIR = ctx["UPDATE_DIR"]

    # --- A. 許願池指令 (/upgrade) ---
    @bot.tree.command(name="upgrade", description="新增功能許願池，將想新增的附帶功能記錄下來")
    @app_commands.describe(feature="想要新增的功能描述（例如：倒數計時、擲骰子、記事本等）")
    async def upgrade_command(interaction: discord.Interaction, feature: str):
        try:
            async with get_file_lock(UPGRADE_FILE):
                os.makedirs(UPDATE_DIR, exist_ok=True)
                with open(UPGRADE_FILE, "a", encoding="utf-8") as f:
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"[{now_str}] {feature}\n")
            await temp_reply(interaction, f"✅ 已將新功能需求紀錄至許願池：\n`{feature}`")
        except Exception as e:
            await temp_reply(interaction, f"❌ 記錄失敗：{e}", ephemeral=True)

    # --- B. 美食推薦抽籤 (/what_to_eat) ---
    @bot.tree.command(name="what_to_eat", description="當主人肚子餓又不知道吃什麼時，讓黛安娜幫忙推薦食物菜單！")
    @app_commands.describe(category="你想選擇的類型 (消暑涼爽 / 在家簡單煮 / 外送推薦 / 隨機抽籤)")
    @app_commands.choices(category=[
        app_commands.Choice(name="❄️ 消暑涼爽（涼麵、冰品、涼拌）", value="cold"),
        app_commands.Choice(name="🏠 在家簡單煮（水餃、乾拌麵、升級泡麵）", value="easy"),
        app_commands.Choice(name="🛵 外送好選擇（便當、漢堡速食、健康餐）", value="delivery"),
        app_commands.Choice(name="🎲 黛安娜隨機特別推薦", value="random")
    ])
    async def what_to_eat_command(interaction: discord.Interaction, category: str = "random"):
        menu_database = {
            "cold": [
                "日式麻醬涼麵 🍜（天氣太熱吃這個最開胃了！）",
                "鮮蝦雞絲沙拉 🥗（清爽又少負擔喔！）",
                "涼拌豆腐皮蛋配白飯 🍚（簡單又冰涼爽口！）",
                "水果冷麵 🍎（酸酸甜甜很消暑！）"
            ],
            "easy": [
                "豪華特製泡麵 🍜（記得要加一顆蛋和一點青菜喔，這樣比較營養！）",
                "手工水餃 🥟（滾水煮幾分鐘就能吃，不用跑出門吹熱風！）",
                "古早味麻醬乾拌麵 🍝（簡單煮又好吃！）",
                "起司咖哩微波飯 🍛（方便又香氣十足！）"
            ],
            "delivery": [
                "多汁炸雞與漢堡套餐 🍔（太熱不想出門就叫外送吹冷氣吃！）",
                "日式豬排定食便當 🍱（飽足感滿滿！）",
                "健康舒肥雞胸肉餐盒 🥗（好吃又健康！）",
                "夏威夷披薩 🍕（可以跟黛安娜一起分著吃！）"
            ]
        }

        if category == "random":
            all_items = [item for sublist in menu_database.values() for item in sublist]
            chosen = random.choice(all_items)
        else:
            chosen = random.choice(menu_database.get(category, menu_database["cold"]))

        msg = (
            f"🍽️ **黛安娜的美食推薦** ✨\n\n"
            f"*(歪頭思考了一下，小跑步跑過來拉拉你的袖子)*\n"
            f"主人主人！黛安娜幫你想好今天可以吃什麼了喔：\n"
            f"👉 **【{chosen}】**\n\n"
            f"主人要記得多喝水，不要餓肚子喔！"
        )
        await send_action_interaction(interaction, msg)

    # --- C. 萬用自訂幸運轉盤 (/wheel) ---
    @bot.tree.command(name="wheel", description="拿不定主意嗎？輸入多個選項讓黛安娜幫你轉幸運轉盤！")
    @app_commands.describe(
        options="請輸入選項（可用逗號、頓號或空白分隔，例如：看電影 打遊戲 睡午覺）",
        title="轉盤主題（可選填，例如：下午要做什麼）"
    )
    async def wheel_command(interaction: discord.Interaction, options: str, title: str = "幸運決定轉盤"):
        items = [x.strip() for x in re.split(r'[,，、/\s]+', options) if x.strip()]
        if len(items) < 2:
            await temp_reply(interaction, "❓ 咦？轉盤至少要有 2 個以上的選項喔！主人可以用空白或逗號把選項分開～", ephemeral=True)
            return

        chosen = random.choice(items)
        options_display = "、".join([f"`{it}`" for it in items])
        msg = (
            f"🎡 **{title}**\n"
            f"*(踮起腳尖用力轉動大轉盤，眼睛亮晶晶地盯著指針停下來)*\n"
            f"📋 候選項目：{options_display}\n"
            f"🎯 轉盤停在 —— **【{chosen}】**！\n"
            f"主人，我們就決定是它了好不好呀？"
        )
        await send_action_interaction(interaction, msg)

    # --- D. 新增定時提醒/鬧鐘 (/remind) ---
    async def remind_time_autocomplete(interaction: discord.Interaction, current: str):
        now = datetime.now()

        def _future(dt):
            return dt if dt > now else dt + timedelta(days=1)

        quick_options = [
            ("10 分鐘後", now + timedelta(minutes=10)),
            ("30 分鐘後", now + timedelta(minutes=30)),
            ("1 小時後", now + timedelta(hours=1)),
            ("今晚 20:00", _future(now.replace(hour=20, minute=0, second=0, microsecond=0))),
            ("明天早上 09:00", (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)),
        ]
        choices = []
        if current.strip():
            choices.append(app_commands.Choice(name=f"直接使用輸入值：{current.strip()}", value=current.strip()))
        for label, dt in quick_options:
            value = dt.strftime("%m-%d %H:%M")
            choices.append(app_commands.Choice(name=f"{label}（{value}）", value=value))
        return choices[:25]

    @bot.tree.command(name="remind", description="新增定時提醒或鬧鐘")
    @app_commands.describe(time="時間格式 (MM-DD HH:MM 或 MM/DD HH:MM)，例如 08/11 09:00", content="提醒內容")
    @app_commands.autocomplete(time=remind_time_autocomplete)
    async def remind_command(interaction: discord.Interaction, time: str, content: str):
        channel_id = str(interaction.channel_id)
        now = datetime.now()

        try:
            time_normalized = time.strip().replace("/", "-")
            target_time = datetime.strptime(time_normalized, "%m-%d %H:%M").replace(year=now.year)
        except ValueError:
            await temp_reply(
                interaction,
                "❌ 咦？黛安娜看不懂這個時間格式耶……要用 `MM-DD HH:MM` 或 `MM/DD HH:MM` 喔，像是 `08/11 09:00` 這樣，主人再打一次看看？"
            )
            return

        if target_time <= now:
            target_time = target_time.replace(year=now.year + 1)

        async with get_file_lock(REMINDER_FILE):
            reminders = await load_json_async(REMINDER_FILE, [])
            reminders.append({
                "channel_id": channel_id,
                "time": target_time.isoformat(),
                "content": content.strip()
            })
            await save_json_async(REMINDER_FILE, reminders)
        await temp_reply(
            interaction,
            f"✅ 黛安娜記下來了！會在 `{target_time.strftime('%Y-%m-%d %H:%M')}` 提醒主人：『{content}』！"
        )

    # --- E. 查看與管理提醒日曆清單 (/reminders) ---
    @bot.tree.command(name="reminders", description="查看目前的提醒與鬧鐘日曆清單，或刪除指定編號的提醒")
    @app_commands.describe(delete_index="若要刪除提醒，請輸入清單上的編號（留空則僅顯示清單）")
    async def reminders_command(interaction: discord.Interaction, delete_index: int = 0):
        now = datetime.now()
        async with get_file_lock(REMINDER_FILE):
            reminders = await load_json_async(REMINDER_FILE, [])
            if not reminders:
                await temp_reply(interaction, "📅 目前沒有任何排程中的提醒或鬧鐘喔！")
                return

            # 依時間排序方便閱讀
            reminders.sort(key=lambda r: parse_reminder_time(r.get("time", ""), now) or now)

            if delete_index > 0:
                if 1 <= delete_index <= len(reminders):
                    removed = reminders.pop(delete_index - 1)
                    await save_json_async(REMINDER_FILE, reminders)
                    t_obj = parse_reminder_time(removed.get("time", ""), now)
                    t_str = t_obj.strftime("%Y-%m-%d %H:%M") if t_obj else removed.get("time", "")
                    await temp_reply(interaction, f"🗑️ 已幫主人取消第 `{delete_index}` 筆提醒：`{t_str}` — 『{removed.get('content', '')}』")
                    return
                else:
                    await temp_reply(interaction, f"❌ 找不到編號 `{delete_index}` 的提醒喔！目前共有 {len(reminders)} 筆提醒。", ephemeral=True)
                    return

        lines = ["📅 **黛安娜的提醒與日曆清單**："]
        for i, item in enumerate(reminders, start=1):
            t_obj = parse_reminder_time(item.get("time", ""), now)
            t_str = t_obj.strftime("%Y-%m-%d %H:%M") if t_obj else item.get("time", "未知時間")
            lines.append(f"`{i}.` ⏰ `{t_str}` — {item.get('content', '')}")
        lines.append("\n💡 *小提示：輸入 `/reminders delete_index:編號` 就可以取消該筆提醒喔！*")
        await temp_reply(interaction, "\n".join(lines))
