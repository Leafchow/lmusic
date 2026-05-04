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
PREFIX = os.getenv("PREFIX", "!")
TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

YDL_OPTIONS = {
    "format": "bestaudio[ext=webm]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
    "extract_flat": False,
}

CONTROL_EMOJIS = ["⏸️", "⏭️", "⏹️", "🔂", "📋"]

# Spotify green brand colour
LMUSIC_COLOR = 0x1DB954
LOADING_COLOR = 0xFFD700
ERROR_COLOR = 0xFF4444


# ─────────────────────────────────────────────────────────────────────────────
#  Per-guild state
# ─────────────────────────────────────────────────────────────────────────────
class GuildState:
    def __init__(self):
        self.queue: deque = deque()
        self.current_track: dict | None = None
        self.current_info: dict | None = None
        self.loop: bool = False
        self.voice_client: discord.VoiceClient | None = None
        self.text_channel: discord.TextChannel | None = None
        self.now_playing_msg: discord.Message | None = None
        self.volume: float = 0.7


_states: dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    if guild_id not in _states:
        _states[guild_id] = GuildState()
    return _states[guild_id]


# ─────────────────────────────────────────────────────────────────────────────
#  Spotify helpers
# ─────────────────────────────────────────────────────────────────────────────
def _spotify_client() -> spotipy.Spotify:
    return spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
        )
    )


def _fetch_playlist_sync(url: str) -> tuple[list[dict], str, str | None]:
    """Blocking — run in executor. Returns (tracks, playlist_name, cover_url)."""
    sp = _spotify_client()
    pid = re.search(r"playlist/([A-Za-z0-9]+)", url).group(1)

    meta = sp.playlist(pid, fields="name,images")
    pl_name = meta.get("name", "Spotify Playlist")
    cover = meta["images"][0]["url"] if meta.get("images") else None

    tracks: list[dict] = []
    results = sp.playlist_tracks(
        pid,
        limit=100,
        fields="items(track(name,artists,album(images))),next",
    )

    while results:
        for item in results.get("items", []):
            t = item.get("track")
            if not t or not t.get("name"):
                continue
            artist = t["artists"][0]["name"] if t.get("artists") else "Unknown"
            img = (
                t["album"]["images"][0]["url"]
                if t.get("album") and t["album"].get("images")
                else None
            )
            tracks.append(
                {
                    "query": f"{t['name']} {artist}",
                    "title": t["name"],
                    "artist": artist,
                    "album_art": img,
                }
            )
        results = sp.next(results) if results.get("next") else None

    return tracks, pl_name, cover


def _fetch_track_sync(url: str) -> dict:
    """Blocking — run in executor. Returns a single track dict."""
    sp = _spotify_client()
    tid = re.search(r"track/([A-Za-z0-9]+)", url).group(1)
    t = sp.track(tid)
    artist = t["artists"][0]["name"] if t.get("artists") else "Unknown"
    img = (
        t["album"]["images"][0]["url"]
        if t.get("album") and t["album"].get("images")
        else None
    )
    return {
        "query": f"{t['name']} {artist}",
        "title": t["name"],
        "artist": artist,
        "album_art": img,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  YouTube helpers
# ─────────────────────────────────────────────────────────────────────────────
async def search_youtube(query: str) -> dict | None:
    """Return yt-dlp info dict for the best YouTube match, or None on failure."""
    loop = asyncio.get_event_loop()

    def _search():
        opts = dict(YDL_OPTIONS)
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)
                if info and "entries" in info and info["entries"]:
                    return info["entries"][0]
            except Exception as exc:
                print(f"[yt-dlp] {exc}")
        return None

    return await loop.run_in_executor(None, _search)


# ─────────────────────────────────────────────────────────────────────────────
#  Embed builders
# ─────────────────────────────────────────────────────────────────────────────
def _duration_str(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_np_embed(
    track: dict,
    info: dict,
    state: GuildState,
    bot_user: discord.ClientUser | None = None,
) -> discord.Embed:
    title = track.get("title") or info.get("title", "Unknown")
    artist = track.get("artist", "")
    yt_url = info.get("webpage_url", "")
    thumbnail = track.get("album_art") or info.get("thumbnail", "")

    desc_lines = [f"### [{title}]({yt_url})"]
    if artist:
        desc_lines.append(f"👤 **{artist}**")

    embed = discord.Embed(
        title="🎵 Now Playing",
        description="\n".join(desc_lines),
        color=LMUSIC_COLOR,
    )

    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    dur = info.get("duration", 0)
    if dur:
        embed.add_field(name="⏱️ Duration", value=_duration_str(int(dur)), inline=True)

    embed.add_field(name="📋 Queue", value=f"{len(state.queue)} tracks", inline=True)
    embed.add_field(name="🔂 Loop", value="✅ On" if state.loop else "Off", inline=True)
    embed.add_field(
        name="🔊 Volume", value=f"{int(state.volume * 100)}%", inline=True
    )

    embed.set_footer(text="⏸️ Pause/Resume  •  ⏭️ Skip  •  ⏹️ Stop  •  🔂 Loop  •  📋 Queue")

    if bot_user:
        embed.set_author(
            name="LMUSIC",
            icon_url=bot_user.display_avatar.url,
        )

    return embed


def build_queue_embed(state: GuildState, page: int = 0) -> discord.Embed:
    embed = discord.Embed(title="📋 Music Queue", color=LMUSIC_COLOR)

    if state.current_track:
        t = state.current_track
        name = t.get("title", t["query"])
        art = t.get("artist", "")
        embed.add_field(
            name="🎵 Now Playing",
            value=f"**{name}**" + (f" — {art}" if art else ""),
            inline=False,
        )

    if state.queue:
        page_size = 15
        tracks = list(state.queue)
        start = page * page_size
        chunk = tracks[start : start + page_size]
        lines = []
        for i, t in enumerate(chunk, start + 1):
            name = t.get("title", t["query"])
            art = t.get("artist", "")
            entry = f"`{i:03d}.` **{name}**" + (f" — {art}" if art else "")
            lines.append(textwrap.shorten(entry, width=80, placeholder="…"))
        embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
        total = len(tracks)
        shown = min((page + 1) * page_size, total)
        embed.set_footer(text=f"Showing {start + 1}–{shown} of {total} tracks")
    else:
        embed.description = "Queue is empty."

    return embed


# ─────────────────────────────────────────────────────────────────────────────
#  Bot setup
# ─────────────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
    case_insensitive=True,
)


# ─────────────────────────────────────────────────────────────────────────────
#  Core playback engine
# ─────────────────────────────────────────────────────────────────────────────
async def _add_controls(msg: discord.Message) -> None:
    for emoji in CONTROL_EMOJIS:
        try:
            await msg.add_reaction(emoji)
        except Exception:
            pass


async def play_next(guild_id: int) -> None:
    state = get_state(guild_id)

    # Loop: push current track back to front
    if state.loop and state.current_track:
        state.queue.appendleft(dict(state.current_track))

    if not state.queue:
        done_embed = discord.Embed(
            title="✅ Queue finished",
            description="All tracks have been played!\nUse `!play` to add more.",
            color=LMUSIC_COLOR,
        )
        if state.now_playing_msg:
            try:
                await state.now_playing_msg.edit(embed=done_embed)
            except Exception:
                pass
        return

    track = state.queue.popleft()
    state.current_track = track

    # ── Show "Searching…" loading embed ──────────────────────────────────────
    loading_embed = discord.Embed(
        title="🔍 Searching YouTube…",
        description=f"**{track.get('title', track['query'])}**"
        + (f"\n👤 {track['artist']}" if track.get("artist") else ""),
        color=LOADING_COLOR,
    )
    if track.get("album_art"):
        loading_embed.set_thumbnail(url=track["album_art"])
    if bot.user:
        loading_embed.set_author(name="LMUSIC", icon_url=bot.user.display_avatar.url)

    if state.now_playing_msg:
        try:
            await state.now_playing_msg.edit(embed=loading_embed)
        except Exception:
            state.now_playing_msg = None

    if not state.now_playing_msg and state.text_channel:
        state.now_playing_msg = await state.text_channel.send(embed=loading_embed)
        await _add_controls(state.now_playing_msg)

    # ── YouTube search ────────────────────────────────────────────────────────
    info = await search_youtube(track["query"])

    if not info:
        if state.text_channel:
            await state.text_channel.send(
                f"⚠️ Skipped **{track.get('title', track['query'])}** — couldn't find on YouTube.",
                delete_after=12,
            )
        await play_next(guild_id)
        return

    state.current_info = info

    # ── Audio playback ────────────────────────────────────────────────────────
    raw = discord.FFmpegPCMAudio(info["url"], **FFMPEG_OPTIONS)
    source = discord.PCMVolumeTransformer(raw, volume=state.volume)

    def _after(error):
        if error:
            print(f"[LMUSIC] Playback error: {error}")
        asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)

    vc = state.voice_client
    if vc and vc.is_connected():
        if vc.is_playing():
            vc.stop()
        vc.play(source, after=_after)

    # ── Update now-playing embed ──────────────────────────────────────────────
    embed = build_np_embed(track, info, state, bot.user)

    if state.now_playing_msg:
        try:
            await state.now_playing_msg.edit(embed=embed)
        except Exception:
            state.now_playing_msg = None

    if not state.now_playing_msg and state.text_channel:
        state.now_playing_msg = await state.text_channel.send(embed=embed)
        await _add_controls(state.now_playing_msg)


# ─────────────────────────────────────────────────────────────────────────────
#  Events
# ─────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅  LMUSIC online — logged in as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name=f"{PREFIX}play | LMUSIC",
        )
    )


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User | discord.Member):
    if user.bot:
        return
    if not reaction.message.guild:
        return

    state = get_state(reaction.message.guild.id)
    if not state.now_playing_msg or reaction.message.id != state.now_playing_msg.id:
        return

    emoji = str(reaction.emoji)
    vc = state.voice_client

    # ── Pause / Resume ──────────────────────────────────────────────────────
    if emoji == "⏸️":
        if vc:
            if vc.is_playing():
                vc.pause()
            elif vc.is_paused():
                vc.resume()

    # ── Skip ────────────────────────────────────────────────────────────────
    elif emoji == "⏭️":
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    # ── Stop ────────────────────────────────────────────────────────────────
    elif emoji == "⏹️":
        state.queue.clear()
        state.loop = False
        state.current_track = None
        if vc:
            vc.stop()
            await vc.disconnect()
            state.voice_client = None
        done_embed = discord.Embed(title="⏹️ Stopped", description="Disconnected.", color=ERROR_COLOR)
        try:
            await state.now_playing_msg.edit(embed=done_embed)
        except Exception:
            pass
        state.now_playing_msg = None
        return  # Don't try to remove reaction from deleted context

    # ── Toggle Loop ─────────────────────────────────────────────────────────
    elif emoji == "🔂":
        state.loop = not state.loop
        if state.current_track and state.current_info:
            embed = build_np_embed(state.current_track, state.current_info, state, bot.user)
            try:
                await state.now_playing_msg.edit(embed=embed)
            except Exception:
                pass
        if state.text_channel:
            await state.text_channel.send(
                f"🔂 Loop **{'enabled ✅' if state.loop else 'disabled'}**",
                delete_after=6,
            )

    # ── Show Queue ──────────────────────────────────────────────────────────
    elif emoji == "📋":
        if state.text_channel:
            if state.queue or state.current_track:
                await state.text_channel.send(embed=build_queue_embed(state), delete_after=30)
            else:
                await state.text_channel.send("📋 Queue is empty!", delete_after=8)

    # Remove the user's reaction to keep controls clean
    try:
        await reaction.remove(user)
    except Exception:
        pass


@bot.event
async def on_voice_state_update(member, before, after):
    """Auto-disconnect when everyone leaves."""
    if member.bot:
        return
    state = get_state(member.guild.id)
    vc = state.voice_client
    if not vc or not vc.is_connected():
        return
    if before.channel == vc.channel and (after.channel != vc.channel):
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans:
            await asyncio.sleep(30)
            # Re-check after wait
            humans = [m for m in vc.channel.members if not m.bot]
            if not humans:
                state.queue.clear()
                state.loop = False
                await vc.disconnect()
                state.voice_client = None
                if state.text_channel:
                    await state.text_channel.send(
                        "👋 Left the voice channel (everyone left).", delete_after=20
                    )


# ─────────────────────────────────────────────────────────────────────────────
#  Commands
# ─────────────────────────────────────────────────────────────────────────────
@bot.command(name="play", aliases=["p"])
async def play_cmd(ctx: commands.Context, *, query: str):
    """Play a YouTube search, Spotify track, or Spotify playlist."""
    if not ctx.author.voice:
        return await ctx.send("❌ You need to be in a voice channel first!")

    state = get_state(ctx.guild.id)
    state.text_channel = ctx.channel

    # Connect / move voice
    vc = state.voice_client
    if not vc or not vc.is_connected():
        state.voice_client = await ctx.author.voice.channel.connect()
    elif ctx.author.voice.channel != vc.channel:
        await vc.move_to(ctx.author.voice.channel)

    already_playing = state.voice_client.is_playing() or state.voice_client.is_paused()

    # ════════════════════════════════════════════════════════════════════════
    #  Spotify Playlist
    # ════════════════════════════════════════════════════════════════════════
    if "open.spotify.com/playlist" in query:
        loading_msg = await ctx.send("⏳ Fetching playlist from Spotify…")

        loop = asyncio.get_event_loop()
        try:
            tracks, pl_name, cover = await loop.run_in_executor(
                None, _fetch_playlist_sync, query
            )
        except Exception as exc:
            return await loading_msg.edit(content=f"❌ Spotify error: `{exc}`")

        if not tracks:
            return await loading_msg.edit(content="❌ Playlist is empty or private.")

        await loading_msg.delete()

        # ── Shuffle / Order prompt ─────────────────────────────────────────
        prompt_embed = discord.Embed(
            title=f"🎵 {pl_name}",
            description=f"**{len(tracks)} tracks** loaded.\n\nHow do you want to play them?",
            color=LMUSIC_COLOR,
        )
        prompt_embed.add_field(name="🔀 Shuffle", value="Random order", inline=True)
        prompt_embed.add_field(name="🔢 In Order", value="Playlist order", inline=True)
        prompt_embed.set_footer(text="React within 30 seconds — defaults to In Order")
        if cover:
            prompt_embed.set_thumbnail(url=cover)
        if bot.user:
            prompt_embed.set_author(name="LMUSIC", icon_url=bot.user.display_avatar.url)

        prompt_msg = await ctx.send(embed=prompt_embed)
        await prompt_msg.add_reaction("🔀")
        await prompt_msg.add_reaction("🔢")

        def _check(r, u):
            return (
                u == ctx.author
                and str(r.emoji) in ("🔀", "🔢")
                and r.message.id == prompt_msg.id
            )

        try:
            reaction, _ = await bot.wait_for("reaction_add", timeout=30.0, check=_check)
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

        if cover:
            result_embed.set_thumbnail(url=cover)
        await prompt_msg.edit(embed=result_embed)

        state.queue.extend(tracks)

        if not already_playing:
            init_embed = discord.Embed(
                title="🎵 LMUSIC", description="Loading first track…", color=LOADING_COLOR
            )
            if bot.user:
                init_embed.set_author(name="LMUSIC", icon_url=bot.user.display_avatar.url)
            state.now_playing_msg = await state.text_channel.send(embed=init_embed)
            await _add_controls(state.now_playing_msg)
            await play_next(ctx.guild.id)
        else:
            await ctx.send(f"➕ Added **{len(tracks)} tracks** to queue.", delete_after=10)

    # ════════════════════════════════════════════════════════════════════════
    #  Single Spotify Track
    # ════════════════════════════════════════════════════════════════════════
    elif "open.spotify.com/track" in query:
        loop = asyncio.get_event_loop()
        try:
            track = await loop.run_in_executor(None, _fetch_track_sync, query)
        except Exception as exc:
            return await ctx.send(f"❌ Spotify error: `{exc}`")

        state.queue.append(track)
        if not already_playing:
            await play_next(ctx.guild.id)
        else:
            await ctx.send(
                f"➕ Added **{track['title']}** by {track['artist']} to queue.",
                delete_after=10,
            )

    # ════════════════════════════════════════════════════════════════════════
    #  YouTube URL or search query
    # ════════════════════════════════════════════════════════════════════════
    else:
        track = {"query": query, "title": query}
        state.queue.append(track)
        if not already_playing:
            await play_next(ctx.guild.id)
        else:
            await ctx.send(f"➕ Added **{query}** to queue.", delete_after=10)


@bot.command(name="skip", aliases=["s", "next"])
async def skip_cmd(ctx: commands.Context):
    """Skip the current track."""
    state = get_state(ctx.guild.id)
    vc = state.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await ctx.message.add_reaction("⏭️")
    else:
        await ctx.send("❌ Nothing is playing.", delete_after=6)


@bot.command(name="pause")
async def pause_cmd(ctx: commands.Context):
    """Pause or resume playback."""
    state = get_state(ctx.guild.id)
    vc = state.voice_client
    if vc:
        if vc.is_playing():
            vc.pause()
            await ctx.message.add_reaction("⏸️")
        elif vc.is_paused():
            vc.resume()
            await ctx.message.add_reaction("▶️")
        else:
            await ctx.send("❌ Nothing is playing.", delete_after=6)


@bot.command(name="resume", aliases=["r", "unpause"])
async def resume_cmd(ctx: commands.Context):
    """Resume paused playback."""
    state = get_state(ctx.guild.id)
    vc = state.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.message.add_reaction("▶️")
    else:
        await ctx.send("❌ Not currently paused.", delete_after=6)


@bot.command(name="stop")
async def stop_cmd(ctx: commands.Context):
    """Stop playback and clear the queue."""
    state = get_state(ctx.guild.id)
    state.queue.clear()
    state.loop = False
    state.current_track = None
    state.current_info = None
    vc = state.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
        state.voice_client = None
    state.now_playing_msg = None
    await ctx.message.add_reaction("⏹️")


@bot.command(name="loop", aliases=["l", "repeat"])
async def loop_cmd(ctx: commands.Context):
    """Toggle loop mode for the current track."""
    state = get_state(ctx.guild.id)
    state.loop = not state.loop
    await ctx.send(f"🔂 Loop **{'enabled ✅' if state.loop else 'disabled'}**.")


@bot.command(name="shuffle")
async def shuffle_cmd(ctx: commands.Context):
    """Shuffle the current queue."""
    state = get_state(ctx.guild.id)
    if not state.queue:
        return await ctx.send("❌ The queue is empty!")
    q_list = list(state.queue)
    random.shuffle(q_list)
    state.queue = deque(q_list)
    await ctx.send(f"🔀 Queue shuffled! **{len(state.queue)} tracks** reordered.", delete_after=10)


@bot.command(name="queue", aliases=["q"])
async def queue_cmd(ctx: commands.Context, page: int = 1):
    """Show the current queue. Use `!queue 2` for page 2."""
    state = get_state(ctx.guild.id)
    if not state.queue and not state.current_track:
        return await ctx.send("📋 Queue is empty!")
    embed = build_queue_embed(state, page=max(0, page - 1))
    await ctx.send(embed=embed)


@bot.command(name="volume", aliases=["vol", "v"])
async def volume_cmd(ctx: commands.Context, vol: int):
    """Set volume (0–100)."""
    if not 0 <= vol <= 100:
        return await ctx.send("❌ Volume must be between **0** and **100**.")
    state = get_state(ctx.guild.id)
    state.volume = vol / 100
    vc = state.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = state.volume
    await ctx.send(f"🔊 Volume set to **{vol}%**", delete_after=10)


@bot.command(name="nowplaying", aliases=["np", "current"])
async def np_cmd(ctx: commands.Context):
    """Show what's currently playing."""
    state = get_state(ctx.guild.id)
    if not state.current_track or not state.current_info:
        return await ctx.send("❌ Nothing is playing right now.")
    embed = build_np_embed(state.current_track, state.current_info, state, bot.user)
    msg = await ctx.send(embed=embed)
    state.now_playing_msg = msg
    await _add_controls(msg)


@bot.command(name="clear", aliases=["wipe"])
async def clear_cmd(ctx: commands.Context):
    """Clear the queue without stopping the current track."""
    state = get_state(ctx.guild.id)
    count = len(state.queue)
    state.queue.clear()
    await ctx.send(f"🗑️ Cleared **{count}** tracks from the queue.", delete_after=10)


@bot.command(name="help", aliases=["h", "commands"])
async def help_cmd(ctx: commands.Context):
    """Show all LMUSIC commands."""
    embed = discord.Embed(
        title="🎵 LMUSIC — Command Reference",
        description="Powered by Spotify metadata + YouTube audio",
        color=LMUSIC_COLOR,
    )

    if bot.user:
        embed.set_author(name="LMUSIC", icon_url=bot.user.display_avatar.url)

    embed.add_field(
        name="▶️ Playback",
        value=(
            f"`{PREFIX}play <query|url>` — YouTube search, Spotify track, or Spotify playlist\n"
            f"`{PREFIX}pause` — Pause / Resume\n"
            f"`{PREFIX}skip` — Skip current track\n"
            f"`{PREFIX}stop` — Stop & disconnect\n"
            f"`{PREFIX}nowplaying` — Show current track\n"
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
        ),
        inline=False,
    )
    embed.add_field(
        name="🎮 Reaction Controls",
        value=(
            "React on the **Now Playing** message:\n"
            "⏸️ Pause/Resume  •  ⏭️ Skip  •  ⏹️ Stop  •  🔂 Loop  •  📋 Queue"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎧 Spotify Support",
        value=(
            "Paste any Spotify **playlist** or **track** URL.\n"
            "Big playlists (500+ tracks) are fully supported.\n"
            "Choose **🔀 Shuffle** or **🔢 In Order** when loading a playlist."
        ),
        inline=False,
    )
    embed.set_footer(text=f"Prefix: {PREFIX}  •  LMUSIC")
    await ctx.send(embed=embed)


# ─────────────────────────────────────────────────────────────────────────────
#  Error handler
# ─────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument. Try `{PREFIX}help`.", delete_after=10)
    elif isinstance(error, commands.CommandNotFound):
        pass  # silently ignore unknown commands
    else:
        print(f"[LMUSIC] Command error in {ctx.command}: {error}")


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set!")
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        print("⚠️  Spotify credentials not set — Spotify URLs will fail.")

    asyncio.run(bot.start(TOKEN))
