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
| `/ecajoin [channel]`                 | Join (your current voice channel by default) and stream. Restarts cleanly if already playing. The bot keeps coming back to this channel after voice drops and restarts until `/ecaleave` or a mod disconnects it. |
| `/ecaleave`                          | Stop streaming and disconnect. Also clears the cloud standby out of the channel. |
| `/ecasearch <query>`                 | Search Apple Music, pick a song from a dropdown, then **Play now**, **Play next**, or **Add to queue**. |
| `/ecaqueue`                          | Show the current song and what's next in Apple Music's queue, grouped like the app: **Playing Next** (added songs), **Continue Playing** (the rest of the album/playlist), then **Autoplay**. Buttons underneath: **Queue** (search for a song to add), **Remove**, **Skip**, and **Refresh** (re-reads the queue into that message; its cooldown doubles on repeated presses, up to 60s). The buttons keep working after a bot restart. |
| `/ecaremove`                         | Pick a song from **Playing Next** (songs people added) and remove it. The album/playlist and Autoplay can't be removed this way. |
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
| `APPLE_MUSIC_LAUNCH` | Command run when WayDroid or Apple Music isn't up. Default: `systemd-run --user --collect --quiet waydroid app launch com.apple.android.music`. |
| `APPLE_MUSIC_LAUNCH_WAIT_SECONDS` | How long to wait for Apple Music's media session after launching it. Default: `90`. |
| `CONTROL_ROLE_IDS` / `CONTROL_USER_IDS` | Who may use the Apple Music control commands. Both empty = everyone. |
| `STANDBY_SSH_HOST` | SSH host running the cloud standby (see below). Empty = no standby. |
| `STANDBY_SSH_COMMAND` | Command run on that host to receive heartbeats. Default: `musicbot-standby/.venv/bin/python musicbot-standby/standby.py relay`. |

### Apple Music control

The control commands don't tap the UI. `src/musicbot/mediactl.dex` (source and `build.sh` in `contrib/mediactl/`) is pushed to WayDroid over ADB and run with `app_process` as the shell user, which holds `MEDIA_CONTENT_CONTROL`; it calls Apple Music's MediaSession directly (`playFromMediaId`, `skipToNext`, `pause`/`play`) and reports whether the track actually changed.

Queueing uses Apple Music's own queue rather than one kept by the bot: MediaCtl sends the app's custom session command `com.apple.android.music.playback.command.ADD_QUEUE_ITEMS` with a `StorePlaybackQueueItemProvider` (a stand-in class with the app's class name and parcel layout, in `contrib/mediactl/com/…`) and an insertion type (`3` = front of Playing Next, `10` = end of Playing Next; not `2`, which appends after Autoplay). **Play now** uses the app's `PLAY_PROVIDER` action with `KEEP_AND_REPLACE` (`6`) rather than `playFromMediaId`: a plain replace makes Apple Music pop a "keep playing or clear the songs you previously queued?" dialog on its own screen whenever Playing Next isn't empty, which nobody in Discord can answer. Songs people queued are kept. The app ignores `10` while Playing Next is empty — it signals this with `EXTRA_CAN_ADD_TO_QUEUE_SECTION=false` in the session extras — so MediaCtl falls back to `3`, which is the same position then. These are app internals found by decompiling Apple Music, so an app update can break them; the bot reports a failure when the visible queue doesn't change. The session only exposes a window of upcoming songs, which is why `/ecaqueue` shows just the next few. With repeat-one on, queued songs never come up.

Search uses the storefront's `music.apple.com/<store>/search` page, because the iTunes Search API returns nothing for some stores (including `cn`). Song IDs are catalog-wide, but availability isn't — a song missing from the account's store won't start, and the bot says so.

WayDroid doesn't need to be running when the bot starts. If ADB can't connect or Apple Music has no media session, the bot runs `APPLE_MUSIC_LAUNCH` (which starts the WayDroid session too), waits for the session, and then retries the command. The failed attempt never reached the app, so nothing runs twice. The launch goes through `systemd-run` so WayDroid lives in its own scope and survives bot restarts.

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

### Muting locally

`contrib/localmute/localmute.sh` stops you hearing WayDroid while the bot keeps streaming it. It retargets WayDroid's stream at BotSink, so WirePlumber drops the link to your speakers or headphones. Run it again to turn local audio back on; WayDroid then follows your default output as usual. It also takes `on`, `off` and `status`.

Muting WayDroid's stream volume would silence the bot too, since stream volume applies before the split. It needs a WayDroid stream to exist, so start playback first.

### Cloud standby

`standby/standby.py` runs on an always-on VM and keeps the bot sitting in its voice channel while this machine is off or offline, so the channel never empties and its "active for" timer doesn't reset. It logs in with the same bot token but only joins and leaves. It plays no audio and ignores commands.

The local bot keeps one `ssh $STANDBY_SSH_HOST` session open and sends a heartbeat with its target channel every 10s. The standby:

- **Holds the channel** if heartbeats stop for 30s. Discord removes a dead session from voice about 80s after it goes silent; measured 2026-10-02. When the standby joins, it takes the slot over before the channel can empty.
- **Joins immediately** when the local bot shuts down cleanly (service stop, reboot). The local bot waits up to 10s for the standby to appear, then exits without leaving voice.
- **Holds the slot during voice reconnects.** discord.py reconnects by leaving and rejoining, which empties the channel for a moment and resets the timer. Re-sending a join for the channel the bot is already in gets no reply from Discord. So the local bot never sends that leave. It asks the standby to take the slot (about 50ms), then joins back. The change of session makes Discord send a fresh voice server. Without a reachable standby, it falls back to leaving and rejoining.
- **Steps aside** when the local bot comes back. The local bot takes the channel on startup or rejoin, and Discord moves the voice session to it.
- **Stays out** after `/ecaleave`, or when a mod disconnects the bot, until the local bot is heard from again.

The target channel is saved in `~/.local/state/musicbot/voice.json`, so the local bot rejoins after restarts on its own.

Both sides log Discord's voice-channel timer events (`voice timer: channel … start_time=…`, where `RESET` means the channel emptied).

Deploy (the VM needs SSH access from this machine and `uv`):

```bash
ssh vm 'mkdir -p ~/musicbot-standby ~/.config/systemd/user'
scp standby/standby.py vm:musicbot-standby/
scp standby/musicbot-standby.service vm:.config/systemd/user/
grep '^DISCORD_TOKEN=' .env | ssh vm 'umask 077; cat > ~/musicbot-standby/.env'
ssh vm 'cd ~/musicbot-standby && uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python "discord.py==2.7.1" \
  && sudo loginctl enable-linger $USER && systemctl --user daemon-reload && systemctl --user enable --now musicbot-standby'
```

Then set `STANDBY_SSH_HOST=vm` in `.env` and restart the bot. The SSH key must work without an agent, because the systemd service has none. `STANDBY_OBSERVE=1` in the VM's `.env` makes the standby log what it would do without joining. On Oracle Linux, user-service logs go to the system journal: `sudo journalctl _SYSTEMD_USER_UNIT=musicbot-standby.service`.

## Troubleshooting

- **"Connecting to voice failed"** — bot lacks `Connect`/`Speak` on the channel, or you're missing `libopus` (most distros bundle it; on Arch: `pacman -S opus`).
- **Bot joins but no sound** — check `pactl list short source-outputs` while playing; ffmpeg should appear as a client of `BotSink.monitor`. Also confirm the Waydroid stream is actually routed to `BotSink` in `pavucontrol`.
- **Bot doesn't show what's playing** — check `playerctl --list-all` works (install `playerctl` if not found) and that `MPRIS_PLAYER` matches the player's bus name or Identity. Presence only shows while the player's status is Playing.
- **Slash commands missing** — set `GUILD_ID` for instant guild sync; global sync can take up to an hour.
