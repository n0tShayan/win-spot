# Win-Spot

A minimal, keyboard-driven Spotify player for the terminal. The UI is Python and
curses; everything heavy lives in a small native engine (`spoterm-engine`, Rust on
top of [librespot](https://github.com/librespot-org/librespot)): it plays the audio
as a Spotify Connect device and makes the Web API calls. You don't need Spotify open
anywhere else: pick a song and press enter.

When SpoTerm is the player, every key press goes straight to the local engine and
player state is pushed back as events, so play, pause, skip, seek and volume act
instantly, and SpoTerm makes **no Web API calls and no polling at all** while music
plays. If another device (your phone, say) is the active one, playing or paused,
SpoTerm controls that instead and plays your picks there.

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

## Download (Windows)

No Python or Rust needed:

1. Download `SpoTerm-<version>-windows-x64.zip` from the
   [Releases](../../releases) page and unzip it anywhere (say `C:\Users\<you>\Apps\SpoTerm`).
2. Double-click **`SpoTerm.exe`**. Pin it to Start or the taskbar to open it with one click.
3. On first run it asks for your Spotify app's **Client ID** (see
   [step 3](#install-from-source) below for making the app) and saves it to
   `C:\Users\<you>\.config\spoterm\.env`. Then come the two browser sign-ins below.

The zip holds `SpoTerm.exe` (the UI with its own Python runtime), `spoterm-engine.exe`
and `_internal\`: keep them together. Each release has a `.sha256` file next to the
zip; check it with `Get-FileHash -Algorithm SHA256 SpoTerm-*.zip`. Windows SmartScreen
may warn about an unsigned app the first time: **More info → Run anyway**.

## Install from source

1. **Python side.**

   ```
   pip install -r requirements.txt
   ```

   That's only `windows-curses` on Windows, and nothing elsewhere.

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
   SpoTerm needs the engine: it also makes the Web API calls. (Set `SPOTERM_ENGINE=0`
   to skip the player and use SpoTerm as a remote only.)

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

   Or `pip install .` once, which gives you a `spoterm` command you can run from anywhere
   (put the engine binary in `~/.config/spoterm/bin/` or on `PATH`, and your `.env` in
   `~/.config/spoterm/`).

## Building the Windows release

```
powershell -ExecutionPolicy Bypass -File build.ps1
```

This builds the engine from `Cargo.lock`, freezes the UI with PyInstaller (pinned in
`packaging/requirements-build.txt`, installed into a private `.venv-build`), puts both
in `dist\SpoTerm\`, smoke-tests it and writes `dist\SpoTerm-<version>-windows-x64.zip`
plus its `.sha256`. Upload those two files to a GitHub release. The engine is built
with the GNU toolchain when it's installed (it links only DLLs that ship with
Windows), otherwise with MSVC and a static C runtime, so users never need the Visual
C++ redistributable. `-SkipEngine` reuses an existing engine build.

It's a one-folder build rather than one self-extracting file on purpose: it starts
instantly (nothing is unpacked to `%TEMP%` each launch) and antivirus flags it far less.

## First run

Two one-time browser sign-ins happen before the UI opens:

1. **SpoTerm** (your app's Client ID, PKCE) for your library, playlists and search.
   It runs as its own short process, so its TLS and HTTP code never load into the UI.
2. **The player** (librespot's own client, port 5588). Spotify only lets that kind of
   client register a Connect device, which is why it's a separate step. Press
   `ctrl+c` to skip it and use SpoTerm as a remote only.

(One sign-in isn't possible: librespot's shared client is rate-limited on the Web
API for everyone, so the library needs your own app.)

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
| `r` | Reload (and retry SpoTerm's player now if it's offline) |
| `?` | Help |
| `q` | Quit |

Playlists play in their own context from the selected track. Liked Songs and search
results play as a track list starting at the selection, so the queue runs through
everything that's loaded.

## Why it's light

Measured on Windows 11 (private memory, as Task Manager shows it):

| | Memory | CPU |
|---|---|---|
| UI (Python) | about 11 MB, of which ~8 MB is the Python interpreter itself | about 0.05% idle |
| Engine, player connected, idle | about 4.5 MB | about 0.05% (Spotify keep-alives) |
| Engine, player off | about 3.5 MB | 0% |

How:

- **The UI loads 46 modules, not 118.** No TLS, sockets, HTTP, `email`, `logging`,
  `dataclasses`, `inspect` or `pathlib`: the engine makes the Web API calls over its
  own HTTPS stack, and the UI uses plain classes and `os.path`.
- **Nothing is polled while SpoTerm plays.** Commands are one line down a pipe; track
  changes, pause, position and volume come back as events. The reader thread blocks
  on the pipe and costs nothing while idle.
- **The sound device is open only while audio plays.** librespot's output normally
  keeps an OS audio stream running (and burning CPU) even when paused; SpoTerm's
  engine opens it on play and releases it on pause.
- **The UI thread sleeps** until there's a key, an event, a finished request or the
  next once-a-second progress tick, and **only changed rows are redrawn**.
- **Other devices** (your phone) are polled adaptively: 5 s while playing and at
  track end, 10 s when paused.

## Security

- Both logins use OAuth with PKCE, so no client secret is needed or stored. SpoTerm's
  callback server listens on loopback only and checks `state`.
- The engine talks only to `api.spotify.com` and `accounts.spotify.com`, with the
  system's TLS (always verified), 10 s timeouts, a 16 MB response cap and no
  redirects. Links to other hosts are refused.
- Nothing from Spotify or the engine is trusted blindly: every field is type-checked
  before use, so malformed data shows as blank or "Unknown", never a crash.
- Everything drawn from Spotify is stripped of control characters, so a track name
  can't inject terminal escape sequences.
- The engine doesn't advertise itself on your network (no discovery), gets an
  environment with SpoTerm's settings and secrets removed, and exits with SpoTerm:
  its stdin closes when SpoTerm exits for any reason, crashes included.
- Tokens are written atomically to the config folder (0600 on POSIX), never logged,
  and never in the repo. `.env` is gitignored.
- `.env` files may only set `SPOTIPY_*`, `SPOTERM_*` and `HTTPS_PROXY`/`NO_PROXY`,
  and are never read from the current folder, so a `.env` planted in whatever folder
  you start SpoTerm from can't point `SPOTERM_ENGINE_BIN` at another program.

## Options

Environment variables, or lines in `.env` (read from the project folder, or the folder
with `SpoTerm.exe`, then the config folder; real environment variables win).

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
| `SPOTERM_DEBUG` | off | `1` logs keys, play decisions and API calls to `spoterm.log` |
| `HTTPS_PROXY` / `NO_PROXY` | none | Proxy for Web API traffic |

## Files

The config folder is `~/.config/spoterm` on every platform (`$XDG_CONFIG_HOME` is
honoured on Linux). On Windows that's `C:\Users\<you>\.config\spoterm`. It isn't
`%APPDATA%` because Microsoft Store Python silently redirects writes there to a
hidden per-app folder.

| Path | Contents |
|---|---|
| `.env` | Your settings, e.g. the Client ID `SpoTerm.exe` asks for on first run |
| `token.json` | SpoTerm's login (the engine refreshes it in place) |
| `engine/` | The player's saved login, device id and volume |
| `engine.log` | The engine's log (overwritten each start) |
| `crash.log` | Only if SpoTerm ever hits a bug: the details, for a bug report |

```
spoterm/        the app (Python)
  app.py        UI state, keys, scheduling, rendering, main()
  engine.py     runs spoterm-engine and speaks its pipe protocol
  api.py        Spotify Web API calls (made by the engine), typed results
  login.py      the one-time sign-in, run as its own process
  auth.py       PKCE login (used only by login.py)
  net.py        HTTPS client for the login (used only by login.py)
  ui.py         theme, glyphs, width-aware text
  worker.py     background job threads
  config.py     settings and .env loading
engine/         spoterm-engine (Rust, librespot 0.8): player + Web API calls
packaging/      PyInstaller entry script and pinned build tools
build.ps1       builds the Windows release zip
pyproject.toml  `pip install .` for a `spoterm` command
```

## Troubleshooting

| Problem | Fix |
|---|---|
| Header says `player not built` | Build the engine (Install from source, step 2). |
| Header says `player: sign-in needed` | Restart SpoTerm; it runs the player sign-in before the UI. |
| Header says `player stopped` | SpoTerm restarts the engine by itself (three tries a minute); press `r` to try again. Details in `engine.log`. |
| Header says `player offline, retrying` | SpoTerm's player can't reach Spotify. A 503 in `engine.log` means Spotify's own playback service is down (not SpoTerm); it retries by itself (at least once a minute), `r` retries now, and other devices can still be controlled. |
| "Spotify lists no active device" while your phone plays | Spotify's servers aren't reporting the phone, which happens during Spotify outages. Check [Spotify Status](https://x.com/SpotifyStatus). |
| No sound, "no audio output device" | Plug in or enable an output device; SpoTerm pauses instead of crashing and plays again when you press play. |
| "Spotify Premium is required" | Playback through third-party apps is Premium-only. |
| "Spotify doesn't let apps list this playlist" | Spotify hides some playlists owned by others from apps. Press enter to play it anyway. |
| Search shows 10 results at a time | Spotify's limit; more load as you scroll. |
| Garbled glyphs | Use Windows Terminal, or set `SPOTERM_ASCII=1`. |
| Start over | Delete `token.json` (SpoTerm login) or `engine/` (player login) in the config folder. |

## Credits

The engine's session and Connect wiring follows [Myx](https://github.com/HaseebKhalid1507/Myx)
(MIT, © Haseeb Khalid) and [spotify-player](https://github.com/aome510/spotify-player)
(MIT, © Thang Pham), and is built on [librespot](https://github.com/librespot-org/librespot).
