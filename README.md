# 🎵 LMUSIC

A Discord music bot that pulls track metadata from **Spotify** and streams audio from **YouTube** — supporting huge playlists, shuffle/order selection, and fully embedded reaction controls.

---

## ✨ Features

- 🎧 **Spotify playlist support** — paste any playlist URL, handles 500+ tracks
- 🔀 **Shuffle or In Order** — choose how to play playlists via reactions
- 🔍 **Best YouTube match** — finds the best audio for every Spotify track
- 🎮 **Reaction controls** — ⏸️ Pause · ⏭️ Skip · ⏹️ Stop · 🔂 Loop · 📋 Queue
- 📋 **Live Now Playing embed** — updates in real time with track info, duration, queue count
- 🔂 **Loop mode** — loop the current track indefinitely
- 🔊 **Volume control** — adjustable per server
- 👋 **Auto-disconnect** — leaves when everyone leaves the voice channel

---

## 🚀 Railway Deployment

### 1. Get your credentials

**Discord Bot Token**
1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. New Application → Bot → Reset Token → copy it
3. Enable these Privileged Intents:
   - ✅ Message Content Intent
   - ✅ Server Members Intent
4. Invite the bot with scopes: `bot` + permissions: `Connect`, `Speak`, `Send Messages`, `Add Reactions`, `Read Message History`, `Embed Links`

**Spotify API Credentials**
1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Create App → Settings → copy **Client ID** and **Client Secret**
3. Add `http://localhost:8888/callback` as a Redirect URI (required even for Client Credentials)

---

### 2. Deploy to Railway

1. Push this project to a GitHub repo
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
3. Add these environment variables in Railway's dashboard:

| Variable | Value |
|---|---|
| `DISCORD_TOKEN` | your Discord bot token |
| `SPOTIFY_CLIENT_ID` | your Spotify client ID |
| `SPOTIFY_CLIENT_SECRET` | your Spotify client secret |
| `PREFIX` | `!` (or your preferred prefix) |

4. Railway will automatically detect `nixpacks.toml` and install `ffmpeg` + all dependencies.
5. The `Procfile` tells Railway to run `python bot.py` as a **worker** (not a web server).

---

## 📖 Commands

| Command | Aliases | Description |
|---|---|---|
| `!play <query\|url>` | `!p` | Play YouTube search, Spotify track, or Spotify playlist |
| `!skip` | `!s`, `!next` | Skip the current track |
| `!pause` | — | Pause or resume playback |
| `!resume` | `!r` | Resume paused playback |
| `!stop` | — | Stop playback and disconnect |
| `!loop` | `!l` | Toggle loop mode |
| `!shuffle` | — | Shuffle the queue |
| `!queue [page]` | `!q` | Show the queue (paginated) |
| `!volume <0-100>` | `!vol` | Set volume |
| `!nowplaying` | `!np` | Show the current track embed |
| `!clear` | — | Clear the queue (keeps current track) |
| `!help` | `!h` | Show all commands |

---

## 🎮 Reaction Controls

React on the **Now Playing** message to control playback without typing:

| Reaction | Action |
|---|---|
| ⏸️ | Pause / Resume |
| ⏭️ | Skip to next track |
| ⏹️ | Stop and disconnect |
| 🔂 | Toggle loop mode |
| 📋 | Show upcoming queue |

---

## 📦 Local Development

```bash
# Clone and enter the project
git clone <your-repo>
cd lmusic

# Install dependencies (requires Python 3.11+)
pip install -r requirements.txt

# Copy and fill in the env file
cp .env.example .env
# edit .env with your credentials

# Run the bot
python bot.py
```

> **Note:** ffmpeg must be installed locally. On Ubuntu: `sudo apt install ffmpeg`. On Mac: `brew install ffmpeg`.

---

## 🎵 Example Usage

```
!play https://open.spotify.com/playlist/5n8K3yxRsqSnlUoYxRkeXK
→ Loads all tracks, asks 🔀 Shuffle or 🔢 In Order
→ Starts playing with a live Now Playing embed + reaction controls

!play never gonna give you up
→ Searches YouTube and plays the best match

!play https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8
→ Finds and plays a single Spotify track
```
