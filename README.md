# musicbot

Bridges a local PipeWire/PulseAudio sink monitor (e.g. Apple Music inside Waydroid, a browser tab, anything) into a Discord voice channel.

## How it works

```
[any app on host] ──► [virtual sink "BotSink"] ──► [BotSink.monitor]
                                                          │
                                                          ▼
                                          ffmpeg ─► discord.py ─► Discord voice
```

The bot doesn't talk to Waydroid directly. You route whatever you want streamed into the `BotSink` sink (via `pavucontrol` or `pw-link`); the bot just reads `BotSink.monitor`.

## Requirements

System packages (Arch names):

| Package          | Used for                                                              |
|------------------|-----------------------------------------------------------------------|
| `ffmpeg`         | Reads the sink monitor and feeds PCM to discord.py.                   |
| `opus`           | Voice encoding (libopus).                                             |
| `pipewire-pulse` | `pactl` for the sink setup; `pw-link` (from `pipewire`) for auto-routing. |
| `playerctl`      | "Now playing" rich presence. **Optional, but without it presence silently does nothing** — no error is logged. |

    sudo pacman -S --needed ffmpeg opus pipewire-pulse playerctl

`busctl` (systemd) is also used by presence to match `MPRIS_PLAYER` against the player's Identity.

## One-time setup

### 1. Create the virtual sink

Quick (lasts until your PipeWire session restarts):

    pactl load-module module-null-sink sink_name=BotSink sink_properties=device.description=BotSink

Persistent (survives reboots):

    mkdir -p ~/.config/pipewire/pipewire.conf.d
    cp contrib/pipewire/null-sink.conf ~/.config/pipewire/pipewire.conf.d/
    systemctl --user restart pipewire pipewire-pulse

Verify:

    pactl list short sinks   | grep BotSink
    pactl list short sources | grep BotSink.monitor

### 2. Route audio into BotSink

Run `pavucontrol` → **Playback** tab → find the Waydroid stream (or whatever app), change its output device to **BotSink**.

If you also want to hear it locally, create a loopback back to your real output:

    pactl load-module module-loopback source=BotSink.monitor sink=<your-real-sink>

### 3. Create the Discord bot

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab → **Reset Token** → copy. (You'll paste it into `.env` in the next step.)
3. **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Connect`, `Speak`, `Use Voice Activity`
4. Open the generated URL and invite the bot to your server.

### 4. Configure and install

    cp .env.example .env
    $EDITOR .env                      # paste DISCORD_TOKEN; set GUILD_ID for instant slash sync
    python -m venv .venv
    source .venv/bin/activate
    pip install -e .

### 5. Run

    ./run.sh

You should see `synced N commands to guild ...` and `logged in as ...`.

## Slash commands

| Command                              | What it does                                                |
|--------------------------------------|-------------------------------------------------------------|
| `/ecaplay [channel]`                 | Join (your current voice channel by default) and stream. Restarts cleanly if already playing. |
| `/ecaleave`                          | Stop streaming and disconnect.                              |
| `/ecasearch <query>`                 | Search Apple Music and pick a song from a dropdown; it starts playing in WayDroid. |
| `/ecaskip`                           | Skip to the next track.                                     |
| `/ecapause`                          | Pause / resume.                                             |

The source is fixed to `AUDIO_SOURCE` from `.env` (default `BotSink.monitor`) — change it there and restart the bot to switch.

## Configuration

| Env var         | Purpose                                                                 |
|-----------------|-------------------------------------------------------------------------|
| `DISCORD_TOKEN` | Bot token from the developer portal. Required.                          |
| `GUILD_IDS`     | Comma-separated guild IDs for instant slash-command sync. Leave blank for global (~1h). |
| `AUDIO_SOURCE`  | PipeWire/Pulse source to read. Default: `BotSink.monitor`.              |
| `OPUS_BITRATE`  | Optional opus bitrate cap in kbps. Default: the server's boost-tier max. See note below. |
| `LOG_LEVEL`     | `DEBUG` / `INFO` / `WARNING`. Default: `INFO`.                          |
| `APPLE_MUSIC_STOREFRONT` | Storefront of the account in the app (e.g. `cn`, `us`). Search results are limited to it. Default: `cn`. |
| `ITUNES_SEARCH_STOREFRONTS` | Stores searched via the iTunes Search API when the storefront's web search can't be parsed; results are then filtered to `APPLE_MUSIC_STOREFRONT`. Default: `us,hk,tw,jp`. |
| `WAYDROID_ADB`  | WayDroid's adbd address. Default: `192.168.240.112:5555`.               |
| `ADB_KEY`       | ADB private key (generated on first use). Default: `~/.config/musicbot/adbkey`. |
| `CONTROL_ROLE_IDS` / `CONTROL_USER_IDS` | Who may use the Apple Music control commands. Both empty = everyone. |

### Apple Music control

The control commands don't tap the UI. `src/musicbot/mediactl.dex` (source and `build.sh` in `contrib/mediactl/`) is pushed to WayDroid over ADB and run with `app_process` as the shell user, which holds `MEDIA_CONTENT_CONTROL`; it calls Apple Music's MediaSession directly (`playFromMediaId`, `skipToNext`, `pause`/`play`) and reports whether the track actually changed.

Search uses the storefront's `music.apple.com/<store>/search` page, because the iTunes Search API returns nothing for some stores (including `cn`). Song IDs are catalog-wide, but availability isn't — a song missing from the account's store won't start, and the bot says so.

First use: WayDroid shows an **Allow USB debugging?** prompt for the bot's key — tick *Always allow* and accept. ADB must be enabled in WayDroid (`waydroid prop get persist.waydroid.adb`, or check that `192.168.240.112:5555` accepts connections).

### Bitrate

The bot sets the opus encoder bitrate to each server's boost-tier maximum and disables FEC (which trades bandwidth for packet-loss resilience — pointless for a music stream). Set `OPUS_BITRATE` to cap it lower across all servers.

| Boost tier     | Channel cap |
|----------------|-------------|
| None           | 96 kbps     |
| Tier 1         | 128 kbps    |
| Tier 2         | 256 kbps    |
| Tier 3 / VIP   | 384 kbps    |

You also need to actually crank the **channel** bitrate to match in Discord: right-click the voice channel → Edit Channel → Audio Bitrate. The wire bitrate is `min(channel_bitrate, encoder_bitrate)` — if the channel is left at the 64 kbps default, that's what you'll hear regardless of the server's tier.

## Troubleshooting

- **"Connecting to voice failed"** — bot lacks `Connect`/`Speak` on the channel, or you're missing `libopus` (most distros bundle it; on Arch: `pacman -S opus`).
- **Bot joins but no sound** — check `pactl list short source-outputs` while playing; ffmpeg should appear as a client of `BotSink.monitor`. Also confirm the Waydroid stream is actually routed to `BotSink` in `pavucontrol`.
- **Bot doesn't show what's playing** — check `playerctl --list-all` works (install `playerctl` if not found) and that `MPRIS_PLAYER` matches the player's bus name or Identity. Presence only shows while the player's status is Playing.
- **Slash commands missing** — set `GUILD_ID` for instant guild sync; global sync can take up to an hour.
