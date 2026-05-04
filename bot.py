"""
██╗     ███╗   ███╗██╗   ██╗███████╗██╗ ██████╗
██║     ████╗ ████║██║   ██║██╔════╝██║██╔════╝
██║     ██╔████╔██║██║   ██║███████╗██║██║
██║     ██║╚██╔╝██║██║   ██║╚════██║██║██║
███████╗██║ ╚═╝ ██║╚██████╔╝███████║██║╚██████╗
╚══════╝╚═╝     ╚═╝ ╚═════╝ ╚══════╝╚═╝ ╚═════╝
LMUSIC — Spotify + YouTube Discord Music Bot
"""

import asyncio
import os
import random
import re
import textwrap
from collections import deque

import discord
import spotipy
import yt_dlp
from discord.ext import commands
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyClientCredentials

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────────────────────────────────────
PREFIX                = os.getenv("PREFIX", "!")
TOKEN                 = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# Primary: iOS client — bypasses YouTube bot-detection on server IPs
YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
    "extractor_args": {
        "youtube": {
            "player_client": ["ios", "web"],
            "player_skip": ["webpage", "configs"],
        }
    },
}

# Fallback: Android client if iOS also fails
YDL_OPTIONS_FALLBACK = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "tv_embedded"],
        }
    },
}

CONTROL_EMOJIS = ["⏸️", "⏭️", "⏹️", "🔂", "📋"]
LMUSIC_COLOR   = 0x1DB954
LOADING_COLOR  = 0xFFD700
ERROR_COLOR    = 0xFF4444


# ─────────────────────────────────────────────────────────────────────────────
#  Per-guild state
# ─────────────────────────────────────────────────────────────────────────────
class GuildState:
    def __init__(self):
        self.queue:           deque                       = deque()
        self.current_track:   dict | None                 = None
        self.current_info:    dict | None                 = None
        self.loop:            bool                        = False
        self.voice_client:    discord.VoiceClient | None  = None
        self.text_channel:    discord.TextChannel | None  = None
        self.now_playing_msg: discord.Message | None      = None
        self.volume:          float                       = 0.7


_states: dict[int, GuildState] = {}

def get_state(guild_id: int) -> GuildState:
    if guild_id not in _states:
        _states[guild_id] = GuildState()
    return _states[guild_id]


# ─────────────────────────────────────────────────────────────────────────────
#  Spotify helpers  (blocking — always run in executor)
# ─────────────────────────────────────────────────────────────────────────────
def _spotify_client() -> spotipy.Spotify:
    return spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
        )
    )

def _fetch_playlist_sync(url: str) -> tuple[list[dict], str, str | None]:
    sp      = _spotify_client()
    pid     = re.search(r"playlist/([A-Za-z0-9]+)", url).group(1)
    meta    = sp.playlist(pid, fields="name,images")
    pl_name = meta.get("name", "Spotify Playlist")
    cover   = meta["images"][0]["url"] if meta.get("images") else None

    tracks: list[dict] = []
    results = sp.playlist_tracks(
        pid, limit=100,
        fields="items(track(name,artists,album(images))),next",
    )
    while results:
        for item in results.get("items", []):
            t = item.get("track")
            if not t or not t.get("name"):
                continue
            artist = t["artists"][0]["name"] if t.get("artists") else "Unknown"
            img    = (t["album"]["images"][0]["url"]
                      if t.get("album") and t["album"].get("images") else None)
            tracks.append({
                "query":     f"{t['name']} {artist}",
                "title":     t["name"],
                "artist":    artist,
                "album_art": img,
            })
        results = sp.next(results) if results.get("next") else None

    return tracks, pl_name, cover

def _fetch_track_sync(url: str) -> dict:
    sp     = _spotify_client()
    tid    = re.search(r"track/([A-Za-z0-9]+)", url).group(1)
    t      = sp.track(tid)
    artist = t["artists"][0]["name"] if t.get("artists") else "Unknown"
    img    = (t["album"]["images"][0]["url"]
              if t.get("album") and t["album"].get("images") else None)
    return {
        "query":     f"{t['name']} {artist}",
        "title":     t["name"],
        "artist":    artist,
        "album_art": img,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  YouTube helpers
# ─────────────────────────────────────────────────────────────────────────────
def _duration_str(seconds) -> str:
    if not seconds:
        return "?:??"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def _ydl_search_sync(q: str, n: int, opts: dict) -> list[dict]:
    """Synchronous yt-dlp search. Returns up to n entries."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch{n}:{q}", download=False)
            if info and "entries" in info:
                return [e for e in info["entries"] if e]
        except Exception as exc:
            print(f"[yt-dlp] '{q}' → {exc}")
    return []

async def search_youtube_multi(query: str, n: int = 10) -> list[dict]:
    """
    Return up to n YouTube results for the search picker.
    Tries iOS client first, falls back to Android.
    """
    loop = asyncio.get_event_loop()
    def _run():
        results = _ydl_search_sync(query, n, YDL_OPTIONS)
        if not results:
            print(f"[yt-dlp] iOS failed for '{query}', trying Android…")
            results = _ydl_search_sync(query, n, YDL_OPTIONS_FALLBACK)
        return results[:n]
    return await loop.run_in_executor(None, _run)

async def search_youtube_best(query: str, title: str = "", artist: str = "") -> dict | None:
    """
    Auto-pick the best YouTube match (used for Spotify auto-play).
    Tries multiple query variations + both player clients.
    """
    loop = asyncio.get_event_loop()

    queries: list[str] = []
    if title and artist:
        queries += [
            f"{title} {artist}",
            f"{title} {artist} official audio",
            f"{title} {artist} lyrics",
            title,
        ]
    else:
        queries += [query, query.split("(")[0].strip()]

    def _pick_best(entries: list) -> dict | None:
        if not entries:
            return None
        tl = title.lower()  if title  else ""
        al = artist.lower() if artist else ""
        filtered = [e for e in entries
                    if not any(x in (e.get("title") or "").lower()
                               for x in ["karaoke", "nightcore", "tutorial", "cover"])]
        pool = filtered or entries
        for e in pool:
            et = (e.get("title") or "").lower()
            if al and al in et: return e
            if tl and tl in et: return e
        return pool[0]

    def _run():
        for q in queries:
            best = _pick_best(_ydl_search_sync(q, 5, YDL_OPTIONS))
            if best: return best
        print(f"[yt-dlp] iOS failed for '{queries[0]}', trying Android fallback…")
        for q in queries:
            best = _pick_best(_ydl_search_sync(q, 5, YDL_OPTIONS_FALLBACK))
            if best: return best
        return None

    return await loop.run_in_executor(None, _run)

async def resolve_url(url: str) -> dict | None:
    """Re-extract a fresh stream URL from a known YouTube page URL."""
    loop = asyncio.get_event_loop()
    def _run():
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            try: return ydl.extract_info(url, download=False)
            except Exception: pass
        with yt_dlp.YoutubeDL(YDL_OPTIONS_FALLBACK) as ydl:
            try: return ydl.extract_info(url, download=False)
            except Exception: pass
        return None
    return await loop.run_in_executor(None, _run)


# ─────────────────────────────────────────────────────────────────────────────
#  Embed builders
# ─────────────────────────────────────────────────────────────────────────────
def build_search_embed(results: list[dict], query: str, prefix: str) -> discord.Embed:
    embed = discord.Embed(
        title="🔍 Search Results",
        color=LMUSIC_COLOR,
    )
    lines = []
    for i, e in enumerate(results, 1):
        title = textwrap.shorten(e.get("title", "Unknown"), width=55, placeholder="…")
        dur   = _duration_str(e.get("duration", 0))
        url   = e.get("webpage_url", "")
        lines.append(f"`{i}.` **[{title}]({url})** `{dur}`")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Type a number 1–{len(results)} to select  •  {prefix}cancel to abort")
    return embed

def build_np_embed(
    track: dict, info: dict, state: GuildState,
    bot_user: discord.ClientUser | None = None,
) -> discord.Embed:
    title     = track.get("title") or info.get("title", "Unknown")
    artist    = track.get("artist", "")
    yt_url    = info.get("webpage_url", "")
    thumbnail = track.get("album_art") or info.get("thumbnail", "")

    desc = [f"### [{title}]({yt_url})"]
    if artist:
        desc.append(f"👤 **{artist}**")

    embed = discord.Embed(title="🎵 Now Playing", description="\n".join(desc), color=LMUSIC_COLOR)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    if info.get("duration"):
        embed.add_field(name="⏱️ Duration", value=_duration_str(info["duration"]), inline=True)
    embed.add_field(name="📋 Queue",  value=f"{len(state.queue)} tracks",      inline=True)
    embed.add_field(name="🔂 Loop",   value="✅ On" if state.loop else "Off",  inline=True)
    embed.add_field(name="🔊 Volume", value=f"{int(state.volume * 100)}%",     inline=True)
    embed.set_footer(text="⏸️ Pause/Resume  •  ⏭️ Skip  •  ⏹️ Stop  •  🔂 Loop  •  📋 Queue")
    if bot_user:
        embed.set_author(name="LMUSIC", icon_url=bot_user.display_avatar.url)
    return embed

def build_queue_embed(state: GuildState, page: int = 0) -> discord.Embed:
    embed = discord.Embed(title="📋 Music Queue", color=LMUSIC_COLOR)
    if state.current_track:
        t = state.current_track
        embed.add_field(
            name="🎵 Now Playing",
            value=f"**{t.get('title', t['query'])}**" + (f" — {t['artist']}" if t.get("artist") else ""),
            inline=False,
        )
    if state.queue:
        page_size = 15
        tracks    = list(state.queue)
        start     = page * page_size
        chunk     = tracks[start : start + page_size]
        lines     = []
        for i, t in enumerate(chunk, start + 1):
            entry = f"`{i:03d}.` **{t.get('title', t['query'])}**" + (f" — {t['artist']}" if t.get("artist") else "")
            lines.append(textwrap.shorten(entry, width=80, placeholder="…"))
        embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
        total = len(tracks)
        shown = min((page + 1) * page_size, total)
        embed.set_footer(text=f"Showing {start + 1}–{shown} of {total} tracks")
    else:
        embed.description = "Queue is empty."
    return embed


# ─────────────────────────────────────────────────────────────────────────────
#  Bot
# ─────────────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.reactions       = True
intents.guilds          = True
intents.voice_states    = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None, case_insensitive=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Playback engine
# ─────────────────────────────────────────────────────────────────────────────
async def _add_controls(msg: discord.Message) -> None:
    for emoji in CONTROL_EMOJIS:
        try:
            await msg.add_reaction(emoji)
        except Exception:
            pass

async def play_next(guild_id: int) -> None:
    state = get_state(guild_id)

    if state.loop and state.current_track:
        state.queue.appendleft(dict(state.current_track))

    if not state.queue:
        embed = discord.Embed(
            title="✅ Queue finished",
            description="All tracks played! Use `!play` to add more.",
            color=LMUSIC_COLOR,
        )
        if state.now_playing_msg:
            try: await state.now_playing_msg.edit(embed=embed)
            except Exception: pass
        return

    track = state.queue.popleft()
    state.current_track = track
    pre_info = track.pop("_yt_info", None)   # pre-resolved info from search picker

    # Loading embed
    loading = discord.Embed(
        title="🎵 Loading…" if pre_info else "🔍 Searching YouTube…",
        description=(
            f"**{track.get('title', track['query'])}**"
            + (f"\n👤 {track['artist']}" if track.get("artist") else "")
        ),
        color=LOADING_COLOR,
    )
    if track.get("album_art"):
        loading.set_thumbnail(url=track["album_art"])
    if bot.user:
        loading.set_author(name="LMUSIC", icon_url=bot.user.display_avatar.url)

    if state.now_playing_msg:
        try: await state.now_playing_msg.edit(embed=loading)
        except Exception: state.now_playing_msg = None

    if not state.now_playing_msg and state.text_channel:
        state.now_playing_msg = await state.text_channel.send(embed=loading)
        await _add_controls(state.now_playing_msg)

    # Resolve audio info
    if pre_info:
        page_url = pre_info.get("webpage_url") or pre_info.get("url", "")
        info = await resolve_url(page_url) or pre_info
    else:
        info = await search_youtube_best(
            track["query"],
            title=track.get("title", ""),
            artist=track.get("artist", ""),
        )

    if not info:
        if state.text_channel:
            await state.text_channel.send(
                f"⚠️ Skipped **{track.get('title', track['query'])}** — couldn't find on YouTube.",
                delete_after=12,
            )
        await play_next(guild_id)
        return

    state.current_info = info

    raw    = discord.FFmpegPCMAudio(info["url"], **FFMPEG_OPTIONS)
    source = discord.PCMVolumeTransformer(raw, volume=state.volume)

    def _after(error):
        if error:
            print(f"[LMUSIC] Playback error: {error}")
        asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)

    vc = state.voice_client
    if vc and vc.is_connected():
        if vc.is_playing(): vc.stop()
        vc.play(source, after=_after)

    embed = build_np_embed(track, info, state, bot.user)
    if state.now_playing_msg:
        try: await state.now_playing_msg.edit(embed=embed)
        except Exception: state.now_playing_msg = None

    if not state.now_playing_msg and state.text_channel:
        state.now_playing_msg = await state.text_channel.send(embed=embed)
        await _add_controls(state.now_playing_msg)


# ─────────────────────────────────────────────────────────────────────────────
#  Voice helper
# ─────────────────────────────────────────────────────────────────────────────
async def _ensure_voice(ctx: commands.Context, state: GuildState) -> bool:
    if not ctx.author.voice:
        await ctx.send("❌ Join a voice channel first!")
        return False
    vc = state.voice_client
    if not vc or not vc.is_connected():
        state.voice_client = await ctx.author.voice.channel.connect()
    elif ctx.author.voice.channel != vc.channel:
        await vc.move_to(ctx.author.voice.channel)
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Events
# ─────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅  LMUSIC online — {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name=f"{PREFIX}play | LMUSIC")
    )

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    state = get_state(member.guild.id)
    vc    = state.voice_client
    if not vc or not vc.is_connected(): return
    if before.channel == vc.channel and after.channel != vc.channel:
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans:
            await asyncio.sleep(30)
            humans = [m for m in vc.channel.members if not m.bot]
            if not humans:
                state.queue.clear()
                state.loop = False
                await vc.disconnect()
                state.voice_client = None
                if state.text_channel:
                    await state.text_channel.send("👋 Left the voice channel (everyone left).", delete_after=20)

@bot.event
async def on_reaction_add(reaction: discord.Reaction, user):
    if user.bot or not reaction.message.guild: return
    state = get_state(reaction.message.guild.id)
    if not state.now_playing_msg or reaction.message.id != state.now_playing_msg.id: return

    emoji = str(reaction.emoji)
    vc    = state.voice_client

    if   emoji == "⏸️":
        if vc:
            if vc.is_playing():  vc.pause()
            elif vc.is_paused(): vc.resume()
    elif emoji == "⏭️":
        if vc and (vc.is_playing() or vc.is_paused()): vc.stop()
    elif emoji == "⏹️":
        state.queue.clear(); state.loop = False; state.current_track = None
        if vc:
            vc.stop(); await vc.disconnect(); state.voice_client = None
        try:
            await state.now_playing_msg.edit(
                embed=discord.Embed(title="⏹️ Stopped", description="Disconnected.", color=ERROR_COLOR)
            )
        except Exception: pass
        state.now_playing_msg = None
        return
    elif emoji == "🔂":
        state.loop = not state.loop
        if state.current_track and state.current_info:
            try:
                await state.now_playing_msg.edit(
                    embed=build_np_embed(state.current_track, state.current_info, state, bot.user)
                )
            except Exception: pass
        if state.text_channel:
            await state.text_channel.send(
                f"🔂 Loop **{'enabled ✅' if state.loop else 'disabled'}**", delete_after=6
            )
    elif emoji == "📋":
        if state.text_channel:
            if state.queue or state.current_track:
                await state.text_channel.send(embed=build_queue_embed(state), delete_after=30)
            else:
                await state.text_channel.send("📋 Queue is empty!", delete_after=8)

    try: await reaction.remove(user)
    except Exception: pass

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument. Try `{PREFIX}help`.", delete_after=10)
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        print(f"[LMUSIC] Error in {ctx.command}: {error}")


# ─────────────────────────────────────────────────────────────────────────────
#  Commands
# ─────────────────────────────────────────────────────────────────────────────
@bot.command(name="play", aliases=["p"])
async def play_cmd(ctx: commands.Context, *, query: str):
    state = get_state(ctx.guild.id)
    state.text_channel = ctx.channel
    if not await _ensure_voice(ctx, state): return

    already_playing = state.voice_client.is_playing() or state.voice_client.is_paused()

    # ── Spotify Playlist ─────────────────────────────────────────────────────
    if "open.spotify.com/playlist" in query:
        loading = await ctx.send("⏳ Fetching playlist from Spotify…")
        try:
            tracks, pl_name, cover = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_playlist_sync, query
            )
        except Exception as exc:
            return await loading.edit(content=f"❌ Spotify error: `{exc}`")

        if not tracks:
            return await loading.edit(content="❌ Playlist is empty or private.")
        await loading.delete()

        prompt_embed = discord.Embed(
            title=f"🎵 {pl_name}",
            description=f"**{len(tracks)} tracks** loaded. How do you want to play them?",
            color=LMUSIC_COLOR,
        )
        prompt_embed.add_field(name="🔀 Shuffle", value="Random order",   inline=True)
        prompt_embed.add_field(name="🔢 In Order", value="Playlist order", inline=True)
        prompt_embed.set_footer(text="React within 30 seconds — defaults to In Order")
        if cover:   prompt_embed.set_thumbnail(url=cover)
        if bot.user: prompt_embed.set_author(name="LMUSIC", icon_url=bot.user.display_avatar.url)

        prompt_msg = await ctx.send(embed=prompt_embed)
        await prompt_msg.add_reaction("🔀")
        await prompt_msg.add_reaction("🔢")

        def _check_order(r, u):
            return u == ctx.author and str(r.emoji) in ("🔀", "🔢") and r.message.id == prompt_msg.id

        try:
            reaction, _ = await bot.wait_for("reaction_add", timeout=30.0, check=_check_order)
            chosen = str(reaction.emoji)
        except asyncio.TimeoutError:
            chosen = "🔢"

        if chosen == "🔀":
            random.shuffle(tracks)
            result_embed = discord.Embed(
                title=f"🔀 Shuffled — {pl_name}",
                description=f"**{len(tracks)} tracks** queued in random order.",
                color=LMUSIC_COLOR,
            )
        else:
            result_embed = discord.Embed(
                title=f"🔢 In Order — {pl_name}",
                description=f"**{len(tracks)} tracks** queued in playlist order.",
                color=LMUSIC_COLOR,
            )
        if cover: result_embed.set_thumbnail(url=cover)
        await prompt_msg.edit(embed=result_embed)

        state.queue.extend(tracks)
        if not already_playing:
            init = discord.Embed(title="🎵 LMUSIC", description="Loading first track…", color=LOADING_COLOR)
            if bot.user: init.set_author(name="LMUSIC", icon_url=bot.user.display_avatar.url)
            state.now_playing_msg = await state.text_channel.send(embed=init)
            await _add_controls(state.now_playing_msg)
            await play_next(ctx.guild.id)
        else:
            await ctx.send(f"➕ Added **{len(tracks)} tracks** to queue.", delete_after=10)

    # ── Single Spotify Track ─────────────────────────────────────────────────
    elif "open.spotify.com/track" in query:
        try:
            track = await asyncio.get_event_loop().run_in_executor(None, _fetch_track_sync, query)
        except Exception as exc:
            return await ctx.send(f"❌ Spotify error: `{exc}`")
        state.queue.append(track)
        if not already_playing:
            await play_next(ctx.guild.id)
        else:
            await ctx.send(f"➕ Added **{track['title']}** by {track['artist']} to queue.", delete_after=10)

    # ── YouTube URL — play directly ──────────────────────────────────────────
    elif re.match(r"https?://(www\.)?(youtube\.com|youtu\.be)/", query):
        track = {"query": query, "title": query}
        state.queue.append(track)
        if not already_playing:
            await play_next(ctx.guild.id)
        else:
            await ctx.send("➕ Added YouTube URL to queue.", delete_after=8)

    # ── Plain search — show 10 results, let user pick ────────────────────────
    else:
        searching_msg = await ctx.send(f"🔍 Searching YouTube for **{query}**…")
        results = await search_youtube_multi(query, n=10)

        if not results:
            return await searching_msg.edit(content="❌ No results found. Try a different search.")

        embed = build_search_embed(results, query, PREFIX)
        await searching_msg.edit(content=None, embed=embed)

        def _check_pick(m: discord.Message) -> bool:
            if m.author != ctx.author or m.channel != ctx.channel:
                return False
            if m.content.lower() in (f"{PREFIX}cancel", "cancel"):
                return True
            return m.content.isdigit() and 1 <= int(m.content) <= len(results)

        try:
            pick_msg = await bot.wait_for("message", timeout=30.0, check=_check_pick)
        except asyncio.TimeoutError:
            return await searching_msg.edit(
                embed=discord.Embed(title="⏰ Timed out", description="Search cancelled.", color=ERROR_COLOR)
            )

        if pick_msg.content.lower() in (f"{PREFIX}cancel", "cancel"):
            await searching_msg.edit(embed=discord.Embed(title="❌ Search cancelled", color=ERROR_COLOR))
            try: await pick_msg.delete()
            except Exception: pass
            return

        idx    = int(pick_msg.content) - 1
        chosen = results[idx]
        try: await pick_msg.delete()
        except Exception: pass

        track = {
            "query":    chosen.get("webpage_url", query),
            "title":    chosen.get("title", query),
            "artist":   chosen.get("uploader", ""),
            "album_art": chosen.get("thumbnail", ""),
            "_yt_info": chosen,
        }

        selected_embed = discord.Embed(
            title="✅ Added to Queue",
            description=(
                f"**[{track['title']}]({chosen.get('webpage_url', '')})**\n"
                f"👤 {track['artist']}  •  ⏱️ {_duration_str(chosen.get('duration', 0))}"
            ),
            color=LMUSIC_COLOR,
        )
        if chosen.get("thumbnail"): selected_embed.set_thumbnail(url=chosen["thumbnail"])
        if bot.user: selected_embed.set_author(name="LMUSIC", icon_url=bot.user.display_avatar.url)
        await searching_msg.edit(embed=selected_embed)

        state.queue.append(track)
        if not already_playing:
            await play_next(ctx.guild.id)


@bot.command(name="skip", aliases=["s", "next"])
async def skip_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    vc    = state.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop(); await ctx.message.add_reaction("⏭️")
    else:
        await ctx.send("❌ Nothing is playing.", delete_after=6)

@bot.command(name="pause")
async def pause_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    vc    = state.voice_client
    if vc:
        if vc.is_playing():   vc.pause();  await ctx.message.add_reaction("⏸️")
        elif vc.is_paused():  vc.resume(); await ctx.message.add_reaction("▶️")
        else: await ctx.send("❌ Nothing is playing.", delete_after=6)

@bot.command(name="resume", aliases=["r", "unpause"])
async def resume_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    vc    = state.voice_client
    if vc and vc.is_paused():
        vc.resume(); await ctx.message.add_reaction("▶️")
    else:
        await ctx.send("❌ Not currently paused.", delete_after=6)

@bot.command(name="stop")
async def stop_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    state.queue.clear(); state.loop = False
    state.current_track = None; state.current_info = None
    vc = state.voice_client
    if vc: vc.stop(); await vc.disconnect(); state.voice_client = None
    state.now_playing_msg = None
    await ctx.message.add_reaction("⏹️")

@bot.command(name="loop", aliases=["l", "repeat"])
async def loop_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    state.loop = not state.loop
    await ctx.send(f"🔂 Loop **{'enabled ✅' if state.loop else 'disabled'}**.")

@bot.command(name="shuffle")
async def shuffle_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    if not state.queue: return await ctx.send("❌ The queue is empty!")
    q_list = list(state.queue); random.shuffle(q_list); state.queue = deque(q_list)
    await ctx.send(f"🔀 Queue shuffled! **{len(state.queue)} tracks** reordered.", delete_after=10)

@bot.command(name="queue", aliases=["q"])
async def queue_cmd(ctx: commands.Context, page: int = 1):
    state = get_state(ctx.guild.id)
    if not state.queue and not state.current_track:
        return await ctx.send("📋 Queue is empty!")
    await ctx.send(embed=build_queue_embed(state, page=max(0, page - 1)))

@bot.command(name="volume", aliases=["vol", "v"])
async def volume_cmd(ctx: commands.Context, vol: int):
    if not 0 <= vol <= 100:
        return await ctx.send("❌ Volume must be 0–100.")
    state = get_state(ctx.guild.id)
    state.volume = vol / 100
    vc = state.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = state.volume
    await ctx.send(f"🔊 Volume set to **{vol}%**", delete_after=10)

@bot.command(name="nowplaying", aliases=["np", "current"])
async def np_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    if not state.current_track or not state.current_info:
        return await ctx.send("❌ Nothing is playing right now.")
    msg = await ctx.send(embed=build_np_embed(state.current_track, state.current_info, state, bot.user))
    state.now_playing_msg = msg
    await _add_controls(msg)

@bot.command(name="clear", aliases=["wipe"])
async def clear_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    count = len(state.queue); state.queue.clear()
    await ctx.send(f"🗑️ Cleared **{count}** tracks from the queue.", delete_after=10)

@bot.command(name="cancel")
async def cancel_cmd(ctx: commands.Context):
    """Cancel an active search picker."""
    await ctx.message.add_reaction("✅")

@bot.command(name="help", aliases=["h", "commands"])
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="🎵 LMUSIC — Command Reference",
        description="Spotify metadata + YouTube audio",
        color=LMUSIC_COLOR,
    )
    if bot.user:
        embed.set_author(name="LMUSIC", icon_url=bot.user.display_avatar.url)
    embed.add_field(
        name="▶️ Playback",
        value=(
            f"`{PREFIX}play <search>` — Shows **10 results** to pick from\n"
            f"`{PREFIX}play <spotify playlist>` — Full playlist with shuffle/order prompt\n"
            f"`{PREFIX}play <spotify track>` — Single Spotify track\n"
            f"`{PREFIX}play <youtube url>` — Direct YouTube URL\n"
            f"`{PREFIX}pause` — Pause / Resume\n"
            f"`{PREFIX}skip` — Skip current track\n"
            f"`{PREFIX}stop` — Stop & disconnect\n"
            f"`{PREFIX}nowplaying` — Refresh Now Playing embed\n"
        ),
        inline=False,
    )
    embed.add_field(
        name="📋 Queue",
        value=(
            f"`{PREFIX}queue [page]` — Show queue\n"
            f"`{PREFIX}shuffle` — Shuffle the queue\n"
            f"`{PREFIX}loop` — Toggle loop mode\n"
            f"`{PREFIX}clear` — Clear queue (keeps current)\n"
            f"`{PREFIX}volume <0-100>` — Adjust volume\n"
            f"`{PREFIX}cancel` — Cancel an active search\n"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎮 Reaction Controls",
        value="React on the **Now Playing** message:\n"
              "⏸️ Pause/Resume  •  ⏭️ Skip  •  ⏹️ Stop  •  🔂 Loop  •  📋 Queue",
        inline=False,
    )
    embed.set_footer(text=f"Prefix: {PREFIX}  •  LMUSIC")
    await ctx.send(embed=embed)


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set!")
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        print("⚠️  Spotify credentials missing — Spotify URLs will not work.")
    asyncio.run(bot.start(TOKEN))