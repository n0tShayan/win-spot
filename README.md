# SpoTerm

A minimal, keyboard-driven Spotify player for the terminal. The UI is Python and
curses; the audio comes from a small built-in Spotify Connect engine
(`spoterm-engine`, Rust on top of [librespot](https://github.com/librespot-org/librespot)).
You don't need Spotify open anywhere else: pick a song and press enter.

When SpoTerm is the player, every key press goes straight to the local engine and
player state is pushed back as events, so play, pause, skip, seek and volume act
instantly, and SpoTerm makes **no Web API calls and no polling at all** while music
plays. If another device (your phone, say) is already playing, SpoTerm controls that
instead and leaves playback where it is.

```
  spoterm   ? help                                                                       ● SpoTerm

  LIBRARY                      Liked Songs                                               30 songs
  Liked Songs
  Search                        #  Title                           Artist                    Time
                                1  Song 0                          Artist                    3:00 ┃
  PLAYLISTS                   ▌ ▶  Song 1                          Artist                    3:00 ┃
  Chill                         3  Song 2                          Artist                    3:00 ┃
  Focus                         4  Song 3                          Artist                    3:00 ┃
                                5  Song 4                          Artist                    3:00

  ────────────────────────────────────────────────────────────────────────────────────────────────
   ▶  Song 1  ♥                                                                  shuffle   repeat
      Artist · Album                                                                 ▁▂▃▄▅▆▇█  45
      0:42  ━━━━━━━━━━━━━━━━━━━━━───────────────────────────────────────────────────────────  3:00
```

Requires **Spotify Premium** (Spotify only allows playback through apps like this on
Premium) and Python 3.10+.

## Install

1. **Python side.**

   ```
   pip install -r requirements.txt
   ```

   That's only `windows-curses` on Windows, and nothing elsewhere: SpoTerm uses the
   standard library for everything else, including HTTPS and OAuth.

2. **The engine** (one-time build, a few minutes). Install Rust with
   [rustup](https://rustup.rs), then from the project folder:

   ```
   cd engine
   cargo build --release
   ```

   On Windows without Visual Studio's C++ build tools, use the GNU toolchain instead
   (needs a MinGW-w64 `gcc` on `PATH`):

   ```
   rustup toolchain install stable-x86_64-pc-windows-gnu
   cargo +stable-x86_64-pc-windows-gnu build --release
   ```

   On Linux the audio backend needs ALSA headers (`libasound2-dev` and `pkg-config`
   on Debian/Ubuntu). SpoTerm finds the binary in `engine/target/release/` by itself.
   Without it, SpoTerm still works as a remote for your other Spotify devices.

3. **A Spotify app for the library** at https://developer.spotify.com/dashboard:
   add the redirect URI `http://127.0.0.1:8888/callback` and copy the **Client ID**
   (no secret needed). Copy `.env.example` to `.env` and put it there:

   ```
   SPOTIPY_CLIENT_ID=your_client_id
   ```

4. **Run.**

   ```
   python -m spoterm
   ```

## First run

Two one-time browser sign-ins happen before the UI opens:

1. **SpoTerm** (your app's Client ID, PKCE) for your library, playlists and search.
2. **The player** (librespot's own client, port 5588). Spotify only lets that kind of
   client register a Connect device, which is why it's a separate step. Press
   `ctrl+c` to skip it and use SpoTerm as a remote only.

After that, `python -m spoterm` starts straight into the UI and the player comes up
in the background in about a second. The header shows `● SpoTerm` when it's ready.

## Keys

| Key | Action |
|---|---|
| `enter` | Open a list / play the selected track |
| `space` `p` | Play / pause (with nothing playing anywhere, plays the selection) |
| `n` / `b` | Next / previous |
| `,` `.` or `shift+←` `shift+→` | Seek 10 s |
| `-` / `+` | Volume |
| `s` | Shuffle |
| `R` | Repeat: off, all, one |
| `f` | Like / unlike the current track |
| `d` | Devices: move playback to another device, or back to SpoTerm |
| `/` | Search (`esc` cancels, `ctrl+u` clears) |
| `tab` | Switch between sidebar and list |
| `←` `→` / `h` `l` | Sidebar / list |
| `[` `]` | Previous / next list |
| `j` `k` `↑` `↓` | Move |
| `PgUp` `PgDn`, `ctrl+u` `ctrl+d`, `g` `G` | Page, half page, top, bottom |
| `r` | Reload |
| `?` | Help |
| `q` | Quit |

Playlists play in their own context from the selected track. Liked Songs and search
results play as a track list starting at the selection, so the queue runs through
everything that's loaded.

## Why it's light

- **Nothing is polled while SpoTerm plays.** Commands are one line down a pipe to the
  engine; track changes, pause, position and volume come back as events. A reader
  thread blocks on the pipe, which costs nothing while idle.
- **The UI thread sleeps** until there's a key, an event, a finished request or the
  next once-a-second progress tick. Progress is interpolated locally.
- **Only changed rows are redrawn.** A progress tick usually rewrites the elapsed time
  and one cell of the bar.
- **Other devices** are polled adaptively (5 s while playing and at track end, 10 s
  when paused), on background threads, with commands on their own thread so they
  never wait behind a poll.
- **Small footprint.** No third-party Python packages; the engine is a 7 MB native
  binary with no audio cache and no periodic position chatter.

Measured on Windows: UI idle CPU about 0.1% of one core; a no-change redraw about
20 µs; Python import about 70 ms.

## Security

- Both logins use OAuth with PKCE, so no client secret is needed or stored. SpoTerm's
  callback server listens on loopback only and checks `state`.
- TLS verification is always on (TLS 1.2+). Responses are size-limited and bad JSON is
  rejected. Pagination links to other hosts are refused.
- Everything drawn from Spotify is stripped of control characters, so a track name
  can't inject terminal escape sequences.
- The engine doesn't advertise itself on your network (no discovery), gets an
  environment with SpoTerm's settings and secrets removed, and exits with SpoTerm:
  its stdin closes, and on Windows a Job Object kills it even if SpoTerm crashes.
- Tokens are written atomically to the config folder (0600 on POSIX), never logged,
  and never in the repo. `.env` is gitignored.
- `.env` files may only set `SPOTIPY_*`, `SPOTERM_*` and `HTTPS_PROXY`/`NO_PROXY`.

## Options

Environment variables, or lines in `.env` (read from the current folder, the project
folder, then the config folder; real environment variables win).

| Variable | Default | Effect |
|---|---|---|
| `SPOTIPY_CLIENT_ID` | required | Your Spotify app's Client ID |
| `SPOTIPY_REDIRECT_URI` | `http://127.0.0.1:8888/callback` | Must match the dashboard |
| `SPOTERM_ENGINE` | on | `0` disables the built-in player |
| `SPOTERM_DEVICE_NAME` | `SpoTerm` | Name of the built-in device |
| `SPOTERM_BITRATE` | `160` | `96`, `160` or `320` |
| `SPOTERM_ENGINE_BIN` | auto | Path to a `spoterm-engine` binary |
| `SPOTERM_ASCII` | off | Plain ASCII glyphs |
| `SPOTERM_TOKEN_PATH` | `<config>/token.json` | Where SpoTerm's login is saved |
| `HTTPS_PROXY` / `NO_PROXY` | none | Proxy for Web API traffic |

## Files

The config folder is `~/.config/spoterm` on every platform (`$XDG_CONFIG_HOME` is
honoured on Linux). On Windows that's `C:\Users\<you>\.config\spoterm`. It isn't
`%APPDATA%` because Microsoft Store Python silently redirects writes there to a
hidden per-app folder.

| Path | Contents |
|---|---|
| `token.json` | SpoTerm's login |
| `engine/` | The player's saved login, device id and volume |
| `engine.log` | The player's log (overwritten each start) |
| `spoterm.log` | SpoTerm's warnings |

```
spoterm/        the app (Python)
  app.py        UI state, keys, scheduling, rendering, main()
  engine.py     runs spoterm-engine and speaks its pipe protocol
  api.py        Spotify Web API, typed results
  auth.py       PKCE login and token refresh
  net.py        HTTPS/JSON client on http.client
  ui.py         theme, glyphs, width-aware text
  worker.py     background job threads
  config.py     settings and .env loading
engine/         spoterm-engine (Rust, librespot 0.8)
```

## Troubleshooting

| Problem | Fix |
|---|---|
| Header says `player not built` | Build the engine (Install, step 2). |
| Header says `player: sign-in needed` | Restart SpoTerm; it runs the player sign-in before the UI. |
| Header says `player stopped` | See `engine.log` in the config folder. |
| "Spotify Premium is required" | Playback through third-party apps is Premium-only. |
| "Spotify doesn't let apps list this playlist" | Spotify hides some playlists owned by others from apps. Press enter to play it anyway. |
| Search shows 10 results at a time | Spotify's limit; more load as you scroll. |
| Garbled glyphs | Use Windows Terminal, or set `SPOTERM_ASCII=1`. |
| Start over | Delete `token.json` (SpoTerm login) or `engine/` (player login) in the config folder. |

## Credits

The engine's session and Connect wiring follows [Myx](https://github.com/HaseebKhalid1507/Myx)
(MIT, © Haseeb Khalid) and [spotify-player](https://github.com/aome510/spotify-player)
(MIT, © Thang Pham), and is built on [librespot](https://github.com/librespot-org/librespot).
