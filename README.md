# SpoTerm — a minimal Spotify client for the terminal

A clean, keyboard-driven Spotify remote that lives in your terminal and
stays at roughly 0.1% CPU while music plays.

```
  spoterm    ? help                                              ● Living Room

  LIBRARY                  Out of My League                            46 songs
  Liked Songs
  Search                    #  Title                        Artist         Time
                            1  Out of My League             Fitz and T…    3:29
  PLAYLISTS                 2  What You Know                Two Door C…    3:11
  Chill                     3  Undercover Martyn            Two Door C…    2:47

  ─────────────────────────────────────────────────────────────────────────────
   ▶  Somebody Else                                  ♥   shuffle   repeat  vol 60%
      The 1975 · I like it when you sleep…
      3:13 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━──────────────────────────── 5:47
```

SpoTerm controls Spotify; it doesn't play audio itself. Keep Spotify open on
any device (desktop app, phone, web player) and SpoTerm drives it.

## Requirements

- Python 3.10+
- **Spotify Premium.** The Web API only allows playback control for Premium accounts.
- A terminal with Unicode support (Windows Terminal, iTerm2, most Linux terminals)

## Setup

1. **Install dependencies**

   ```
   pip install -r requirements.txt
   ```

2. **Create a Spotify app** at https://developer.spotify.com/dashboard
   - Add the Redirect URI `http://127.0.0.1:8888/callback`
   - Enable **Web API**, then copy the **Client ID** and **Client Secret**

3. **Add your credentials.** Copy `.env.example` to `.env` and fill it in:

   ```
   SPOTIPY_CLIENT_ID=...
   SPOTIPY_CLIENT_SECRET=...
   SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback
   ```

   `.env` is git-ignored. SpoTerm reads it from the current folder, the
   project folder, or the config folder (`%APPDATA%\spoterm` on Windows,
   `~/.config/spoterm` elsewhere).

4. **Run**

   ```
   python -m spoterm
   ```

   The first run opens your browser to log in. The login token is saved to
   the config folder, not the project, so it can't be committed by accident.

## Keys

| Key | Action |
|---|---|
| `space` / `p` | Play / pause |
| `n` / `b` | Next / previous track |
| `,` / `.` | Seek back / forward 10s |
| `-` / `+` | Volume down / up |
| `s` / `r` | Shuffle / cycle repeat (off, all, one) |
| `f` | Like / unlike the current track |
| `d` | Choose playback device |
| `/` | Search |
| `enter` | Open list / play track |
| `tab`, `h`, `l` | Move between sidebar and track list |
| `j` `k`, `↑` `↓`, `PgUp` `PgDn`, `g` `G` | Navigate |
| `R` | Refresh |
| `?` | Help |
| `q` | Quit |

## Why it's light

- The UI thread sleeps until there's input, a finished request, or the next
  once-a-second progress tick. There's no busy loop.
- Progress is interpolated locally, so Spotify is polled every 5s while
  playing (and right at track end) and every 10s when paused.
- Each second only the progress line is redrawn. Everything else is redrawn
  only when it changes.
- All network calls run on two background threads, so the UI never freezes.
- Long lists load page by page as you scroll.

## Options

| Variable | Effect |
|---|---|
| `SPOTERM_ASCII=1` | Use plain ASCII glyphs for fonts without box-drawing characters |
| `SPOTERM_TOKEN_PATH` | Custom location for the saved login token |

## Troubleshooting

| Problem | Fix |
|---|---|
| "No active device" | Open Spotify on any device, or press `d` to pick one |
| "Premium is required" | Playback control is Premium-only on Spotify's side |
| A playlist says Spotify won't list it | Spotify blocks apps from reading some playlists owned by others. Press `enter` to play it anyway |
| Garbled glyphs | Use Windows Terminal rather than the legacy console, or set `SPOTERM_ASCII=1` |
| Something odd happened | Check `spoterm.log` in the config folder |
