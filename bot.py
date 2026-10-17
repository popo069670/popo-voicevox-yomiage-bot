"""Zunda Bot - Discord 読み上げBot (VOICEVOX エンジン利用)

必要なもの: Python 3.11+, FFmpeg, VOICEVOX エンジン(ローカル)
設定は .env に書きます(README 参照)。
"""
import asyncio
import io
import json
import os
import re
import time
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN が設定されていません。.env を確認してください")

ENGINE_URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")

OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)          # あなたのDiscordユーザーID(/status用)
LOG_WEBHOOK_URL = os.environ.get("LOG_WEBHOOK_URL", "")        # 通知用Webhook(任意)
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")        # 死活監視URL(任意)
START_TIME = time.time()

LEAVE_SECONDS = 90      # 誰もいなくなってから抜けるまでの秒数
MAX_CHARS = 30          # これを超えたら「以下省略」
QUEUE_MAX = 15          # 読み上げ待ちの上限
USER_COOLDOWN = 1.5     # 同じ人の連続投稿を読まない間隔(秒)
DICT_MAX = 100          # 1人あたりの辞書登録数の上限

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
USERS_FILE = DATA_DIR / "users.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

charas = {
    "ずんだもん": 3, "四国めたん": 2, "春日部つむぎ": 8, "雨晴はう": 10,
    "波音リツ": 9, "玄野武宏": 11, "冥鳴ひまり": 14,
}


# ---------- 保存まわり ----------
def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json(path, data):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


users = load_json(USERS_FILE)        # {user_id: {"speed": 1.0, "dict": {単語: 読み}}}
settings = load_json(SETTINGS_FILE)  # {guild_id: {"auto_channel", "pitch", "speaker"}}


def get_cfg(guild_id):
    return settings.setdefault(str(guild_id), {})


def get_user(user_id):
    return users.setdefault(str(user_id), {})


# ---------- Bot 本体 ----------
class Bot(discord.Client):
    def __init__(self):
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        self.watchdog_task = asyncio.create_task(watchdog())


client = Bot()
channels = {}       # guild_id -> 読み上げ対象テキストch
queues = {}         # guild_id -> asyncio.Queue(bytes)
workers = {}
leave_tasks = {}
connecting = set()
last_msg = {}


def apply_dict(user_id, text):
    if user_id is None:
        return text
    words = users.get(str(user_id), {}).get("dict", {})
    for w in sorted(words, key=len, reverse=True):
        text = text.replace(w, words[w])
    return text


async def speak(guild, text, user_id=None):
    q = queues.get(guild.id)
    if q is None or q.full():
        return
    text = apply_dict(user_id, text)[:200]
    cfg = get_cfg(guild.id)
    speed = users.get(str(user_id), {}).get("speed", 1.0) if user_id else 1.0
    speaker = cfg.get("speaker", 3)
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{ENGINE_URL}/audio_query",
                              params={"text": text, "speaker": speaker}) as r:
                if r.status != 200:
                    print("audio_queryエラー:", r.status)
                    return
                query = await r.json()
            query["speedScale"] = float(speed)
            query["pitchScale"] = float(cfg.get("pitch", 0.0))
            async with s.post(f"{ENGINE_URL}/synthesis",
                              params={"speaker": speaker}, json=query) as r:
                if r.status != 200:
                    print("synthesisエラー:", r.status)
                    return
                data = await r.read()
        if not data or len(data) > 5_000_000:
            return
        if queues.get(guild.id) is not q or q.full():
            return
        q.put_nowait(data)
    except Exception as e:
        print("音声生成エラー:", type(e).__name__)


state = {"started_notified": False}


async def notify(text):
    """プライベートチャンネルのWebhookに通知(スマホのDiscordに届く)"""
    if not LOG_WEBHOOK_URL:
        return
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            await s.post(LOG_WEBHOOK_URL, json={
                "content": text[:1900],
                "allowed_mentions": {"parse": []},
            })
    except Exception as e:
        print("通知エラー:", type(e).__name__)


async def engine_version():
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"{ENGINE_URL}/version") as r:
                if r.status == 200:
                    return (await r.text()).strip().strip('"')
    except Exception:
        pass
    return None


async def watchdog():
    """60秒ごとにエンジンを確認。状態が変わったら通知し、死活監視URLにも知らせる"""
    await asyncio.sleep(10)
    was_ok = None
    while True:
        ok = (await engine_version()) is not None
        if (was_ok is None and not ok) or (was_ok is not None and ok != was_ok):
            await notify("✅ VOICEVOXエンジンが復旧しました" if ok
                         else "⚠️ VOICEVOXエンジンに接続できません")
        was_ok = ok
        if HEALTHCHECK_URL:
            url = HEALTHCHECK_URL if ok else HEALTHCHECK_URL.rstrip("/") + "/fail"
            try:
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as s:
                    await s.get(url)
            except Exception:
                pass
        await asyncio.sleep(60)


async def worker(guild):
    q = queues[guild.id]
    loop = asyncio.get_running_loop()
    while True:
        data = await q.get()
        vc = guild.voice_client
        if not vc:
            continue
        done = asyncio.Event()

        def after(err, done=done):
            loop.call_soon_threadsafe(done.set)

        try:
            vc.play(discord.FFmpegPCMAudio(io.BytesIO(data), pipe=True), after=after)
        except Exception as e:
            print("再生エラー:", type(e).__name__)
            continue
        await done.wait()


async def start_session(guild, voice_channel, text_channel_id):
    await voice_channel.connect()
    channels[guild.id] = text_channel_id
    queues[guild.id] = asyncio.Queue(maxsize=QUEUE_MAX)
    workers[guild.id] = asyncio.create_task(worker(guild))
    await speak(guild, "接続しました")


async def cleanup(guild):
    t = leave_tasks.pop(guild.id, None)
    if t and t is not asyncio.current_task():
        t.cancel()
    w = workers.pop(guild.id, None)
    if w:
        w.cancel()
    queues.pop(guild.id, None)
    if guild.voice_client:
        await guild.voice_client.disconnect()


async def auto_leave(guild):
    await asyncio.sleep(LEAVE_SECONDS)
    vc = guild.voice_client
    if vc and not any(not m.bot for m in vc.channel.members):
        ch = guild.get_channel(channels.get(guild.id))
        await cleanup(guild)
        if ch:
            try:
                await ch.send("誰もいなくなったので抜けるのだ！")
            except Exception:
                pass


# ---------- イベント ----------
@client.event
async def on_ready():
    print(f"{client.user} が起動したのだ！")
    if not state["started_notified"]:
        state["started_notified"] = True
        await notify(f"🟢 Botが起動しました({client.user})")
    for g in client.guilds:
        tc = get_cfg(g.id).get("auto_channel")
        if not tc or g.voice_client:
            continue
        for vch in g.voice_channels:
            if any(not m.bot for m in vch.members):
                try:
                    await start_session(g, vch, tc)
                except Exception as e:
                    print("自動参加エラー:", type(e).__name__)
                break


@client.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    guild = member.guild
    vc = guild.voice_client
    cfg = get_cfg(guild.id)

    if not vc:
        tc = cfg.get("auto_channel")
        if tc and after.channel and after.channel != before.channel and guild.id not in connecting:
            connecting.add(guild.id)
            try:
                await start_session(guild, after.channel, tc)
            except Exception as e:
                print("自動参加エラー:", type(e).__name__)
            finally:
                connecting.discard(guild.id)
        return

    bot_ch = vc.channel
    joined = after.channel == bot_ch and before.channel != bot_ch
    left = before.channel == bot_ch and after.channel != bot_ch

    if joined:
        await speak(guild, f"{member.display_name}さんが入室しました", member.id)
    elif left:
        await speak(guild, f"{member.display_name}さんが退出しました", member.id)

    humans = [m for m in bot_ch.members if not m.bot]
    if humans:
        t = leave_tasks.pop(guild.id, None)
        if t:
            t.cancel()
    elif guild.id not in leave_tasks:
        leave_tasks[guild.id] = asyncio.create_task(auto_leave(guild))


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    g = message.guild
    vc = g.voice_client
    if not vc or channels.get(g.id) != message.channel.id:
        return
    voice = getattr(message.author, "voice", None)
    if not voice or voice.channel != vc.channel:
        return
    now = time.monotonic()
    if now - last_msg.get(message.author.id, 0) < USER_COOLDOWN:
        return
    last_msg[message.author.id] = now
    text = message.clean_content
    text = re.sub(r"https?://\S+", "リンク省略", text)
    text = re.sub(r"<a?:\w+:\d+>", "", text)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "、以下省略"
    if not text.strip():
        return
    await speak(g, text, message.author.id)


# ---------- コマンド ----------
@client.tree.command(name="join", description="ボイスチャンネルにボットを呼ぶのだ")
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        return await interaction.response.send_message("先にボイスチャンネルに入ってほしいのだ！")
    g = interaction.guild
    if g.voice_client or g.id in connecting:
        return await interaction.response.send_message("もう接続しているのだ！")
    await interaction.response.defer()
    connecting.add(g.id)
    try:
        await start_session(g, interaction.user.voice.channel, interaction.channel_id)
    except Exception as e:
        print("接続エラー:", type(e).__name__)
        connecting.discard(g.id)
        return await interaction.followup.send("接続できなかったのだ。")
    connecting.discard(g.id)
    await interaction.followup.send("お待たせしたのだ！このチャンネルを読み上げるのだ。")


@client.tree.command(name="leave", description="ボットがバイバイするのだ")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    g = interaction.guild
    vc = g.voice_client
    if not vc:
        return await interaction.response.send_message("まだボイスチャンネルに入っていないのだ。")
    u = interaction.user
    same = bool(u.voice and u.voice.channel == vc.channel)
    if not same and not u.guild_permissions.manage_guild:
        return await interaction.response.send_message("Botと同じVCにいる人だけが使えるのだ。")
    await cleanup(g)
    await interaction.response.send_message("バイバイなのだー！")


@client.tree.command(name="auto_on", description="このチャンネルを読み上げ先にして自動参加をオンにするのだ")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def auto_on(interaction: discord.Interaction):
    get_cfg(interaction.guild.id)["auto_channel"] = interaction.channel_id
    save_json(SETTINGS_FILE, settings)
    await interaction.response.send_message("自動参加をオンにしたのだ！")


@client.tree.command(name="auto_off", description="自動参加をオフにするのだ")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def auto_off(interaction: discord.Interaction):
    get_cfg(interaction.guild.id).pop("auto_channel", None)
    save_json(SETTINGS_FILE, settings)
    await interaction.response.send_message("自動参加をオフにしたのだ。")


@client.tree.command(name="speed", description="あなたの読み上げ速度(0.5〜2.0、普通は1.0)")
@app_commands.guild_only()
async def speed(interaction: discord.Interaction, value: app_commands.Range[float, 0.5, 2.0]):
    get_user(interaction.user.id)["speed"] = value
    save_json(USERS_FILE, users)
    await interaction.response.send_message(
        f"{interaction.user.display_name}さんの速さを {value} にしたのだ！", ephemeral=True)


@client.tree.command(name="pitch", description="読み上げの高さ(-0.15〜0.15、サーバー共通)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def pitch(interaction: discord.Interaction, value: app_commands.Range[float, -0.15, 0.15]):
    get_cfg(interaction.guild.id)["pitch"] = value
    save_json(SETTINGS_FILE, settings)
    await interaction.response.send_message(f"高さを {value} にしたのだ！")


@client.tree.command(name="chara", description="読み上げるキャラクターを変更するのだ")
@app_commands.guild_only()
@app_commands.choices(name=[app_commands.Choice(name=n, value=v) for n, v in charas.items()])
async def chara(interaction: discord.Interaction, name: app_commands.Choice[int]):
    get_cfg(interaction.guild.id)["speaker"] = name.value
    save_json(SETTINGS_FILE, settings)
    await interaction.response.send_message(f"読み上げキャラクターを【{name.name}】に変更したのだ！")


@client.tree.command(name="dic_add", description="あなた専用の辞書に登録するのだ")
@app_commands.guild_only()
@app_commands.describe(word="変換したい単語", reading="読み方")
async def dic_add(interaction: discord.Interaction,
                  word: app_commands.Range[str, 1, 20],
                  reading: app_commands.Range[str, 1, 40]):
    d = get_user(interaction.user.id).setdefault("dict", {})
    if word not in d and len(d) >= DICT_MAX:
        return await interaction.response.send_message("辞書がいっぱいなのだ。", ephemeral=True)
    d[word] = reading
    save_json(USERS_FILE, users)
    await interaction.response.send_message(f"登録したのだ！【{word}】→【{reading}】", ephemeral=True)


@client.tree.command(name="dic_remove", description="あなたの辞書から削除するのだ")
@app_commands.guild_only()
@app_commands.describe(word="削除する単語")
async def dic_remove(interaction: discord.Interaction, word: str):
    d = users.get(str(interaction.user.id), {}).get("dict", {})
    if word in d:
        del d[word]
        save_json(USERS_FILE, users)
        return await interaction.response.send_message(f"【{word}】を削除したのだ！", ephemeral=True)
    await interaction.response.send_message("その単語は登録されていないのだ。", ephemeral=True)


@client.tree.command(name="dic_list", description="あなたの辞書の一覧を見るのだ")
@app_commands.guild_only()
async def dic_list(interaction: discord.Interaction):
    d = users.get(str(interaction.user.id), {}).get("dict", {})
    if not d:
        return await interaction.response.send_message("まだ何も登録されていないのだ。", ephemeral=True)
    lines = [f"{w} → {r}" for w, r in d.items()]
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


@client.tree.command(name="status", description="Botの状態を見るのだ(管理者のみ)")
async def status(interaction: discord.Interaction):
    if not OWNER_ID or interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("使えないのだ。", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    ver = await engine_version()
    up = int(time.time() - START_TIME)
    h, rem = divmod(up, 3600)
    sessions = [
        f"・{g.name}: {g.voice_client.channel.name}(待ち {queues[g.id].qsize()})"
        for g in client.guilds if g.voice_client and g.id in queues
    ]
    lines = [
        "**Botの状態**",
        f"稼働時間: {h}時間{rem // 60}分",
        f"遅延: {round(client.latency * 1000)}ms",
        f"サーバー数: {len(client.guilds)}",
        f"エンジン: {'OK ' + ver if ver else '接続できません'}",
        f"読み上げ中: {len(sessions)}件",
    ] + sessions
    await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)


@client.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    name = interaction.command.name if interaction.command else "?"
    print("コマンドエラー:", name, type(error).__name__)
    await notify(f"⚠️ コマンドエラー: /{name} ({type(error).__name__})")
    try:
        if interaction.response.is_done():
            await interaction.followup.send("エラーが起きたのだ。", ephemeral=True)
        else:
            await interaction.response.send_message("エラーが起きたのだ。", ephemeral=True)
    except Exception:
        pass


if __name__ == "__main__":
    client.run(TOKEN)
