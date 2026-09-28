# SpoTerm

SpoTerm is a minimal, keyboard-driven Spotify client for the terminal, written in
Python with curses. It can play music on its own: it runs a Spotify Connect device
called "SpoTerm" (a managed [librespot](https://github.com/librespot-org/librespot)
process), so you don't need another Spotify app open. If something is already playing
on another device, SpoTerm controls that device and leaves playback where it is. It
is built to stay light, with an idle UI that uses a small fraction of one percent of
a CPU core. Apart from `windows-curses` on Windows, it uses only the standard library.

```
  spoterm   ? help                                        engine ready   ● Living Room Speaker

  LIBRARY                     Liked Songs                                           200 songs
  Liked Songs
  Search                        #  Title                         Artist                  Time
                                1  City Summer                   Moon                    5:45 ┃
  PLAYLISTS                     2  Echo                          Love River              1:46
  Rain Echo                     3  Love                          Love Echo Dream         6:19
  Echo Moon Road Gold           ▶  Wild Road                     Road River Gold         6:21
  City                          5  Blue Heart Gold Blue Rain L…  Dream Gold Gold         5:01
  Moon Blue                  ▌  6  Heart Wild Wild City          Blue Heart              1:52
  Summer Gold Gold              7  Echo Heart Heart              Road Love Dream         6:32
  Blue Fire                     8  River Summer Love             Fire Dream              5:54
  Night River Moon Rain         9  Moon Wild Light Wild          Rain                    2:18
  Fire Love Dream              10  Summer Night City City Love   Love Summer             3:06
  City River Gold              11  Gold City Heart               Summer                  5:28

  ────────────────────────────────────────────────────────────────────────────────────────────
   ▶  Wild Road  ♥                                                           shuffle   repeat
      Road River Gold · Light                                                    ▁▂▃▄▅▆▇█  64
      1:23  ━━━━━━━━━━━━━━━━───────────────────────────────────────────────────────────  6:21
```

<sub>Rendered at 22x96 from an offline stub with placeholder data. The header shows
SpoTerm's own player ("engine ready") next to the device that is currently playing.</sub>

## Contents

- [Requirements](#requirements)
- [Installing librespot](#installing-librespot)
- [Setup](#setup)
- [First run](#first-run)
- [Keys](#keys)
- [Options](#options)
- [Performance](#performance)
- [Security](#security)
- [Files](#files)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)

## Requirements

- Python 3.10+
- **Spotify Premium.** Spotify only allows playback, and playback control through
  the Web API, on Premium accounts.
- A terminal with Unicode support, at least 60x14. Windows Terminal, iTerm2 and most
  Linux terminals work. If yours doesn't, set `SPOTERM_ASCII=1`.
- Optional: **librespot**, for the built-in "SpoTerm" device. Without it, SpoTerm is
  only a remote for your other Spotify devices.

## Installing librespot

SpoTerm starts librespot itself with `--backend rodio`, so the default build (which
includes rodio) is what you need. It was developed against librespot 0.8.

librespot is written in Rust. Install Rust with [rustup](https://rustup.rs) first.

### Windows

On Windows, rustup sets up the MSVC toolchain by default. If Visual Studio's "Desktop
development with C++" workload isn't installed, the build fails at the linking step.
Pick one of these two routes:

**A. MSVC.** Install the Visual Studio C++ build tools with the "Desktop development
with C++" workload, then run:

```
cargo install librespot --locked
```

**B. GNU toolchain (the route used during development).** Put a MinGW-w64 `gcc` on
`PATH`, then run:

```
rustup toolchain install stable-x86_64-pc-windows-gnu
cargo +stable-x86_64-pc-windows-gnu install librespot --locked
```

Either way, the build takes about 10 minutes.

### macOS and Linux

These steps weren't tested for SpoTerm:

```
cargo install librespot --locked
```

On Linux, the audio backend needs the ALSA development headers, for example
`libasound2-dev` and `pkg-config` on Debian/Ubuntu or `alsa-lib-devel` on Fedora.

### Where SpoTerm looks for it

SpoTerm checks these places, in this order:

1. `SPOTERM_LIBRESPOT`, if set. The path must point to the file itself. If the file
   isn't there, SpoTerm treats librespot as not installed and doesn't check the other
   places.
2. `librespot` on `PATH`
3. `~/.cargo/bin`, which is where `cargo install` puts it
4. `<config_dir>/bin` (see [Files](#files))

## Setup

1. **Install the dependencies.**

   ```
   pip install -r requirements.txt
   ```

   On Windows this installs `windows-curses`. On other systems there is nothing to
   install.

2. **Create a Spotify app** at https://developer.spotify.com/dashboard.
   - Add the redirect URI `http://127.0.0.1:8888/callback`.
   - Copy the **Client ID**. You don't need the client secret: SpoTerm logs in with
     PKCE.

3. **Add your Client ID.** Copy `.env.example` to `.env` and fill it in:

   ```
   SPOTIPY_CLIENT_ID=your_client_id
   SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback
   ```

   You can leave `SPOTIPY_CLIENT_SECRET` out. SpoTerm never uses it to log in. It is
   used only to refresh an older token that was issued to the confidential client
   (one saved without the PKCE flag, such as a spotipy cache). Tokens from SpoTerm's
   own login are always refreshed without it.

4. **Run SpoTerm.**

   ```
   python -m spoterm
   ```

## First run

Both logins happen once, before the UI starts.

1. **Your SpoTerm login.** Your browser opens Spotify's login page. You can also
   visit the URL that SpoTerm prints. SpoTerm waits up to 5 minutes for the redirect
   on `127.0.0.1:8888`. If it can't listen there, for example because the port is
   busy or the redirect URI isn't a loopback `http` address, paste the full URL you
   were redirected to into the terminal instead. On later runs, SpoTerm refreshes the
   saved token before the UI starts. If Spotify has revoked the token, you log in
   again.

2. **The built-in player's login** (only if librespot is installed and not yet
   signed in):
   - SpoTerm first tries a silent login by giving librespot its own access token,
     which includes the `streaming` scope. It passes the token in an environment
     variable, never on the command line. This attempt waits up to 20 seconds.
   - If Spotify refuses the token, librespot runs its own OAuth login. A browser
     opens, and SpoTerm prints the URL to visit. librespot's default callback port
     for this is 5588. SpoTerm waits up to 5 minutes.
   - Press `ctrl+c` to skip this step. SpoTerm then only controls your other devices.

   librespot saves reusable credentials in `<config_dir>/librespot`. After that, the
   player starts silently in the background each time SpoTerm starts. The header
   shows its state: `engine starting…`, `engine ready`, `engine login needed`,
   `engine not installed` or `engine exited (N)`.

When you play something and Spotify has no active device, SpoTerm uses its own
player. It waits up to 20 seconds for the player to register, and restarts it once if
it has died. If no SpoTerm device appears, it falls back to the first available
device. To move playback somewhere else, press `d`.

## Keys

| Key | Action |
|---|---|
| `space` / `p` | Play / pause |
| `n` / `b` | Next / previous track |
| `,` `<` `shift+←` / `.` `>` `shift+→` | Seek back / forward 10 s |
| `-` `_` / `+` `=` | Volume down / up 5% (key repeats are merged into one request) |
| `s` | Toggle shuffle |
| `r` | Cycle repeat: off, all, one |
| `f` | Like / unlike the current track |
| `d` | Devices: `j` `k` to choose, `enter` to move playback there, `esc` to close |
| `/` | Search: type, then `enter`. `esc` cancels. `ctrl+u` clears. `ctrl+a` / `ctrl+e` jump to start / end |
| `enter` | Open the selected list, or play the selected track |
| `tab` / `shift+tab` | Switch between the sidebar and the track list |
| `h` `←` / `l` `→` | Go to the sidebar / open the selected sidebar item |
| `j` `↓` / `k` `↑` | Down / up |
| `PgDn` / `PgUp` | Page down / up |
| `ctrl+d` / `ctrl+u` | Half page down / up |
| `g` `Home` / `G` `End` | Top / bottom |
| `R` | Refresh playlists and the current list |
| `?` | Help. `esc`, `q` or `?` closes it |
| `q` | Quit |

If you play a track from Liked Songs, it plays in the Liked Songs context, so the
queue continues from there. If you play a track from a playlist, it plays at that
track's position in the playlist. A search result plays as a list of up to 100
tracks, starting with the selected one. Local files show dimmed, and you can't play
them through the API.

## Options

Set these as environment variables or in a `.env` file. Flags count as on when they
are `1`, `true`, `yes` or `on`.

| Variable | Default | Effect |
|---|---|---|
| `SPOTIPY_CLIENT_ID` | (required) | Your Spotify app's Client ID |
| `SPOTIPY_REDIRECT_URI` | `http://127.0.0.1:8888/callback` | Must match the redirect URI in the dashboard |
| `SPOTIPY_CLIENT_SECRET` | (empty) | Used only to refresh tokens that weren't issued through PKCE |
| `SPOTERM_TOKEN_PATH` | `<config_dir>/token.json` | Where the login token is saved |
| `SPOTERM_ASCII` | off | Plain ASCII glyphs, for fonts without box-drawing characters |
| `SPOTERM_ENGINE` | on | Run the built-in librespot player. Set it to `0` to use SpoTerm only as a remote |
| `SPOTERM_DEVICE_NAME` | `SpoTerm` | Name of the built-in Spotify Connect device |
| `SPOTERM_BITRATE` | `160` | Built-in player bitrate: `96`, `160` or `320`. Any other value means `160` |
| `SPOTERM_LIBRESPOT` | (search) | Full path to the librespot binary |
| `HTTPS_PROXY` / `NO_PROXY` | (none) | Sends API traffic through a plain `http://` proxy. `user:pass@` is supported. `NO_PROXY` takes host suffixes or `*` |

**.env files.** SpoTerm reads `.env` from these folders, in this order:

1. the current folder
2. the project folder, meaning the folder that contains the `spoterm/` package
3. the config folder

Variables already set in the environment take priority. After that, the first file
that defines a key wins. SpoTerm reads only `SPOTIPY_*`, `SPOTERM_*`, `HTTPS_PROXY`
and `NO_PROXY` (in either case) from these files and ignores everything else. That
way, a stray `.env` file can't set variables like `PATH` or `SSL_CERT_FILE`. The
files are parsed as plain `KEY=VALUE` lines. Nothing in them is expanded or run.

## Performance

These were measured on Windows:

| | |
|---|---|
| UI CPU while a track plays | about 0.03-0.1% of one core |
| Import time | about 65-75 ms (down from about 250 ms) |
| SpoTerm memory | about 16 MB private (down from about 32 MB). A bare Python process has a working set of about 22 MB |
| librespot memory | about 15 MB working set, 4 MB private |
| librespot CPU while decoding | not measured. Decoding audio has a cost, so expect more than the UI alone |
| Redraw when nothing changed | about 20 µs |
| Progress tick | about 3 µs of SpoTerm's own work (measured on a stub screen). About 50 µs including the actual curses write |

How SpoTerm keeps its usage low:

- **It sleeps until something happens.** The UI thread blocks in `getch` until there
  is input, a finished request, the next once-a-second progress tick or a timer.
  There is no busy loop.
- **It interpolates progress locally**, so it doesn't need to ask Spotify for the
  playback position all the time.
- **It polls adaptively.** While playing, it polls every 5 s, and again right when
  the track should end. While paused or idle, it polls every 10 s. After an error, it
  waits 15 s, or longer if Spotify sends a `Retry-After`. After a command, it runs a
  few quick re-checks.
- **It redraws only the rows that changed.** Each row remembers what it last drew. A
  progress tick usually rewrites just the elapsed time and one cell of the bar.
- **It keeps network I/O on two worker threads**, one for playback and one for
  library and search. The UI never waits on the network, and it needs no locks,
  because all state lives on the UI thread.
- **Its HTTP client uses only the standard library.** It is a small client built on
  `http.client`, with one keep-alive connection per thread and gzip.
- **It loads pages lazily.** Long lists load page by page as you scroll.

## Security

- **Login:** OAuth Authorization Code with PKCE (S256), so no client secret is
  needed. SpoTerm generates a random `state` and checks it with a constant-time
  comparison. The callback server binds only to loopback (`127.0.0.1` or `::1`),
  never to all interfaces, and a login attempt times out after 5 minutes.
- **TLS:** certificates and hostnames are always verified against the system trust
  store, with TLS 1.2 as the minimum. There is no setting to turn verification off.
- **Token storage:** the token is written atomically (to a temporary file, then
  renamed) in the config folder, not in the project folder. On POSIX, the file is
  `0600` and the folder `0700`. On Windows, the file inherits the per-user
  `%APPDATA%` permissions.
- **Logs:** SpoTerm never logs tokens or secrets. Its own log only records warnings.
- **Terminal output:** every name that comes from Spotify (tracks, artists, albums,
  playlists, devices, error messages) has non-printable characters removed before it
  is drawn. A track name can't inject terminal escape sequences.
- **Response limits:** responses are limited to 16 MB, both before and after gzip
  decompression. Invalid or deeply nested JSON is rejected instead of crashing
  SpoTerm. Pagination links that point to another host are refused.
- **librespot:**
  - It runs with discovery off (`--disable-discovery`, no mDNS), so it doesn't
    advertise itself on your LAN.
  - It gets a scrubbed environment: `SPOTIPY_*`, `SPOTERM_*` and `LIBRESPOT_*`
    variables are removed.
  - The token for its silent login is passed in the environment, never in `argv`.
  - It writes only to `librespot.log`, and it stops when SpoTerm stops. On Windows,
    a Job Object kills it even if SpoTerm crashes.
- **Secrets in git:** `.env` (and `.env.*`, except `.env.example`) is gitignored, and
  so is spotipy's `.cache`.

## Files

`<config_dir>` is `%APPDATA%\spoterm` on Windows. Elsewhere it is
`~/.config/spoterm`, or `$XDG_CONFIG_HOME/spoterm` if that variable is set.

| Path | Contents |
|---|---|
| `<config_dir>/token.json` | SpoTerm's login token (or the path in `SPOTERM_TOKEN_PATH`) |
| `<config_dir>/spoterm.log` | Warnings and errors from SpoTerm |
| `<config_dir>/librespot.log` | Output of the most recent librespot run (overwritten each start) |
| `<config_dir>/librespot/` | librespot's cached credentials and volume. No audio is cached |
| `<config_dir>/.env` | Optional settings (see [Options](#options)) |
| `<config_dir>/bin/` | Optional place for the librespot binary |

Project layout:

```
spoterm/
  __main__.py   entry point for python -m spoterm
  app.py        UI state, key handling, scheduling, rendering, main()
  ui.py         theme, glyphs, width-aware text fitting
  api.py        Spotify Web API calls, returning small typed objects
  auth.py       PKCE login, token file, thread-safe refresh
  net.py        HTTPS/JSON client on http.client (keep-alive, retries, limits)
  engine.py     the managed librespot process
  worker.py     background job threads
  config.py     settings and .env loading
requirements.txt
.env.example
```

## Troubleshooting

| Problem | Fix |
|---|---|
| "No active device", or the SpoTerm device doesn't appear | Check the `engine …` label in the header. **not installed:** install librespot, or set `SPOTERM_LIBRESPOT`. **login needed:** delete `<config_dir>/librespot/` and restart, which runs the one-time login again. **exited (N):** see `librespot.log`. You can also open Spotify on another device and press `d`. |
| "Spotify Premium is required" | Spotify only allows playback and playback control on Premium accounts. |
| "Spotify doesn't let apps list this playlist" | Spotify blocks apps from reading some playlists, such as many owned by others. Press `enter` to play it anyway. |
| Search shows only 10 results at a time | SpoTerm asks for 10 per page because Spotify rejects larger search limits. More results load as you scroll. |
| librespot build fails with a linker error (`link.exe`) | The MSVC toolchain has no C++ build tools. Use route A or B under [Installing librespot](#installing-librespot). On Linux, install the ALSA dev headers and `pkg-config`. |
| Garbled or missing glyphs | Use Windows Terminal instead of the legacy console, or set `SPOTERM_ASCII=1`. |
| Something else went wrong | Check `spoterm.log` and `librespot.log` in the config folder. |
| Reset the login | Delete `<config_dir>/token.json` for SpoTerm's login, or `<config_dir>/librespot/` for the built-in player. Then restart. |

## Known limitations

- librespot is an unofficial client. Spotify may change something that breaks it,
  and then the built-in player stops working until librespot is updated. SpoTerm
  keeps working as a remote in the meantime.
- The developer hasn't yet tested the full browser logins (SpoTerm's and
  librespot's) or actual audio output from start to finish.
- On macOS and Linux, librespot is stopped by an `atexit` handler. If SpoTerm is
  killed outright, librespot can keep running. Only Windows has the Job Object
  guarantee.
- If librespot's saved credentials stop working, SpoTerm doesn't log it in again by
  itself. Delete `<config_dir>/librespot/` to run the login again.
