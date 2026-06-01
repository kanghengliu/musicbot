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
| `/play [channel] [source]`           | Join (your current voice channel by default) and stream.    |
| `/stop`                              | Stop streaming, stay connected.                             |
| `/join [channel]`                    | Join without playing.                                       |
| `/leave`                             | Disconnect.                                                 |

`source` defaults to `AUDIO_SOURCE` in `.env` (e.g. `BotSink.monitor`). Pass any other PipeWire/Pulse source name to override per-call (`pactl list short sources` to enumerate).

## Configuration

| Env var         | Purpose                                                                 |
|-----------------|-------------------------------------------------------------------------|
| `DISCORD_TOKEN` | Bot token from the developer portal. Required.                          |
| `GUILD_ID`      | Single guild for instant slash-command sync. Leave blank for global.    |
| `AUDIO_SOURCE`  | PipeWire/Pulse source to read. Default: `BotSink.monitor`.              |
| `OPUS_BITRATE`  | Opus encoder bitrate in kbps. Default: `256`. See note below.           |
| `LOG_LEVEL`     | `DEBUG` / `INFO` / `WARNING`. Default: `INFO`.                          |

### Bitrate

The bot sets the opus encoder bitrate from `OPUS_BITRATE` (kbps) and disables FEC (which trades bandwidth for packet-loss resilience — pointless for a music stream).

Discord caps **per voice channel** based on boost tier:

| Boost tier | Channel cap |
|------------|-------------|
| None       | 96 kbps     |
| Tier 1     | 128 kbps    |
| Tier 2     | 256 kbps    |
| Tier 3     | 384 kbps    |

You also need to actually crank the **channel** bitrate to match in Discord: right-click the voice channel → Edit Channel → Audio Bitrate. The wire bitrate is `min(channel_bitrate, OPUS_BITRATE)` — if the channel is left at the 64 kbps default, that's what you'll hear regardless of `OPUS_BITRATE`.

## Troubleshooting

- **"Connecting to voice failed"** — bot lacks `Connect`/`Speak` on the channel, or you're missing `libopus` (most distros bundle it; on Arch: `pacman -S opus`).
- **Bot joins but no sound** — check `pactl list short source-outputs` while playing; ffmpeg should appear as a client of `BotSink.monitor`. Also confirm the Waydroid stream is actually routed to `BotSink` in `pavucontrol`.
- **Slash commands missing** — set `GUILD_ID` for instant guild sync; global sync can take up to an hour.
