//! spoterm-engine: SpoTerm's built-in Spotify Connect player.
//!
//! `spoterm-engine login --cache DIR` runs the one-time browser login and stores
//! reusable credentials in DIR.
//!
//! `spoterm-engine run --cache DIR [--name NAME] [--bitrate 96|160|320] [--volume PCT]`
//! brings up the Connect device and speaks JSON lines: commands on stdin, events on
//! stdout, logs on stderr. Playback commands go straight to the local player, so they
//! take effect instantly with no Web API round trip. Closing stdin shuts the player
//! down, so it never outlives SpoTerm.
//!
//! Exit codes: 0 normal, 1 error, 3 login needed.
//!
//! Session and Spirc wiring follows Myx (MIT, © Haseeb Khalid) and spotify-player
//! (MIT, © Thang Pham).

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use librespot_connect::{
    ConnectConfig, LoadContextOptions, LoadRequest, LoadRequestOptions, Options as CtxOptions,
    PlayingTrack, Spirc,
};
use librespot_core::cache::Cache;
use librespot_core::config::DeviceType;
use librespot_core::{authentication::Credentials, Session, SessionConfig};
use librespot_metadata::audio::UniqueFields;
use librespot_oauth::OAuthClientBuilder;
use librespot_playback::audio_backend;
use librespot_playback::config::{AudioFormat, Bitrate, PlayerConfig};
use librespot_playback::mixer::softmixer::SoftMixer;
use librespot_playback::mixer::{Mixer, MixerConfig};
use librespot_playback::player::{Player, PlayerEvent};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};

/// librespot's public desktop client id. Spotify only lets this kind of client
/// register a Connect device; a developer-dashboard client id gets
/// "Login request was denied: INVALID_CREDENTIALS".
const CLIENT_ID: &str = "65b708073fc0480ea92a077233ca87bd";
const REDIRECT_URI: &str = "http://127.0.0.1:5588/login";
const SCOPES: &[&str] = &[
    "streaming",
    "app-remote-control",
    "user-read-private",
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-library-read",
    "user-library-modify",
    "playlist-read-private",
    "playlist-read-collaborative",
];
const EXIT_LOGIN: i32 = 3;
const HEALTH_CHECK: Duration = Duration::from_secs(5);
const RETRY_MIN: Duration = Duration::from_secs(5);
const RETRY_MAX: Duration = Duration::from_secs(120);

type Tx = UnboundedSender<Value>;

struct Opts {
    cache: PathBuf,
    name: String,
    bitrate: Bitrate,
    volume_pct: u8,
}

fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("warn"))
        .target(env_logger::Target::Stderr)
        .init();
    let result = parse_args().and_then(|(mode, opts)| match mode.as_str() {
        "login" => login(&opts),
        "run" => run(opts),
        _ => bail!("usage: spoterm-engine login|run --cache DIR [--name N] [--bitrate 96|160|320] [--volume PCT]"),
    });
    if let Err(e) = result {
        eprintln!("spoterm-engine: {e:#}");
        std::process::exit(1);
    }
}

fn parse_args() -> Result<(String, Opts)> {
    let mut args = std::env::args().skip(1);
    let mode = args.next().unwrap_or_default();
    let mut opts = Opts {
        cache: PathBuf::new(),
        name: "SpoTerm".into(),
        bitrate: Bitrate::Bitrate160,
        volume_pct: 50,
    };
    while let Some(flag) = args.next() {
        let value = args.next().with_context(|| format!("{flag} needs a value"))?;
        match flag.as_str() {
            "--cache" => opts.cache = PathBuf::from(value),
            "--name" => opts.name = value,
            "--bitrate" => {
                opts.bitrate = match value.as_str() {
                    "96" => Bitrate::Bitrate96,
                    "320" => Bitrate::Bitrate320,
                    _ => Bitrate::Bitrate160,
                }
            }
            "--volume" => opts.volume_pct = value.parse::<u8>().unwrap_or(50).min(100),
            _ => bail!("unknown option {flag}"),
        }
    }
    if opts.cache.as_os_str().is_empty() {
        bail!("--cache is required");
    }
    Ok((mode, opts))
}

fn runtime() -> Result<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .context("start async runtime")
}

fn open_cache(dir: &Path) -> Result<Cache> {
    std::fs::create_dir_all(dir).context("create cache dir")?;
    Cache::new(Some(dir), Some(dir), None, None).map_err(|e| anyhow!("open cache: {e}"))
}

/// A stable device id, so Spotify sees one "SpoTerm" device rather than a new one per launch.
fn session_config(dir: &Path) -> SessionConfig {
    let mut cfg = SessionConfig {
        client_id: CLIENT_ID.into(),
        ..SessionConfig::default()
    };
    let path = dir.join("device_id");
    match std::fs::read_to_string(&path) {
        Ok(id) if id.trim().len() >= 16 => cfg.device_id = id.trim().to_string(),
        _ => {
            let _ = std::fs::write(&path, &cfg.device_id);
        }
    }
    cfg
}

// ── Login ────────────────────────────────────────────────────────────────────
fn login(o: &Opts) -> Result<()> {
    let client = OAuthClientBuilder::new(CLIENT_ID, REDIRECT_URI, SCOPES.to_vec())
        .open_in_browser()
        .with_custom_message("SpoTerm is signed in. You can close this tab and go back to the terminal.")
        .build()
        .map_err(|e| anyhow!("{e}"))?;
    let token = client.get_access_token().map_err(|e| anyhow!("login failed: {e}"))?;
    runtime()?.block_on(async {
        let session = Session::new(session_config(&o.cache), Some(open_cache(&o.cache)?));
        // store_credentials=true swaps the one-shot token for reusable credentials.
        session
            .connect(Credentials::with_access_token(token.access_token), true)
            .await
            .map_err(|e| anyhow!("sign-in failed: {e}"))?;
        println!("Signed in as {}.", session.username());
        Ok(())
    })
}

// ── Run ──────────────────────────────────────────────────────────────────────
/// Everything that dies with the access-point connection and is rebuilt on reconnect.
struct Link {
    spirc: Spirc,
    player: Arc<Player>,
    session: Session,
}

#[derive(Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum Cmd {
    PlayContext {
        uri: String,
        #[serde(default)]
        track_uri: Option<String>,
        #[serde(default)]
        index: Option<u32>,
        #[serde(default)]
        position_ms: u32,
        #[serde(default)]
        shuffle: bool,
    },
    PlayTracks {
        uris: Vec<String>,
        #[serde(default)]
        start_uri: Option<String>,
        #[serde(default)]
        position_ms: u32,
    },
    Play,
    Pause,
    Toggle,
    Next,
    Prev,
    Seek { ms: u32 },
    Volume { pct: u8 },
    Shuffle { on: bool },
    Repeat { mode: String },
    Activate,
    Token {
        #[serde(default)]
        id: u64,
    },
    Quit,
}

fn run(o: Opts) -> Result<()> {
    runtime()?.block_on(async move {
        let (tx, rx) = unbounded_channel();
        tokio::spawn(writer(rx));

        let cache = open_cache(&o.cache)?;
        if cache.credentials().is_none() {
            login_needed(&tx, "not signed in").await;
        }
        let mixer = Arc::new(SoftMixer::open(MixerConfig::default()).map_err(|e| anyhow!("mixer: {e}"))?);
        mixer.set_volume(cache.volume().unwrap_or(from_pct(o.volume_pct)));

        let mut link = match connect(&o, &mixer, &tx).await {
            Ok(link) => link,
            Err(e) if is_credentials_error(&e) => {
                let _ = std::fs::remove_file(o.cache.join("credentials.json"));
                login_needed(&tx, "Spotify rejected the saved login").await;
                unreachable!()
            }
            Err(e) => return Err(e),
        };
        send(&tx, json!({
            "ev": "ready",
            "device_id": link.session.device_id(),
            "name": o.name,
            "volume": to_pct(mixer.volume()),
        }));

        let mut lines = BufReader::new(tokio::io::stdin()).lines();
        let mut health = tokio::time::interval(HEALTH_CHECK);
        health.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let (mut backoff, mut retry_at) = (RETRY_MIN, Instant::now());

        loop {
            tokio::select! {
                line = lines.next_line() => {
                    let Ok(Some(line)) = line else { break };   // EOF: SpoTerm has gone
                    if line.trim().is_empty() {
                        continue;
                    }
                    match serde_json::from_str::<Cmd>(&line) {
                        Ok(Cmd::Quit) => break,
                        Ok(cmd) => handle(&link, &mixer, &tx, cmd),
                        Err(e) => send(&tx, json!({"ev": "error", "cmd": "parse", "msg": e.to_string()})),
                    }
                }
                _ = health.tick() => {
                    // librespot invalidates the session when a keep-alive goes unanswered and
                    // leaves recovery to us, so rebuild it with backoff.
                    if !link.session.is_invalid() || Instant::now() < retry_at {
                        continue;
                    }
                    send(&tx, json!({"ev": "reconnecting"}));
                    match connect(&o, &mixer, &tx).await {
                        Ok(fresh) => {
                            let dead = std::mem::replace(&mut link, fresh);
                            let _ = dead.spirc.shutdown();
                            backoff = RETRY_MIN;
                            send(&tx, json!({"ev": "reconnected", "device_id": link.session.device_id()}));
                        }
                        Err(e) => {
                            log::warn!("reconnect failed: {e:#}");
                            retry_at = Instant::now() + backoff;
                            backoff = (backoff * 2).min(RETRY_MAX);
                        }
                    }
                }
            }
        }
        let _ = link.spirc.shutdown();
        link.player.stop();
        tokio::time::sleep(Duration::from_millis(200)).await;   // let the device say goodbye
        Ok(())
    })
}

async fn connect(o: &Opts, mixer: &Arc<SoftMixer>, tx: &Tx) -> Result<Link> {
    let cache = open_cache(&o.cache)?;
    let creds = cache.credentials().context("not signed in")?;
    let session = Session::new(session_config(&o.cache), Some(cache));
    let backend = audio_backend::find(None).context("no audio backend available")?;
    let config = PlayerConfig {
        bitrate: o.bitrate,
        ..Default::default()   // no periodic position events: SpoTerm interpolates locally
    };
    let player = Player::new(config, session.clone(), mixer.get_soft_volume(), move || {
        backend(None, AudioFormat::default())
    });

    let mut events = player.get_player_event_channel();
    let etx = tx.clone();
    tokio::spawn(async move {
        while let Some(ev) = events.recv().await {
            if let Some(v) = event_json(ev) {
                if etx.send(v).is_err() {
                    break;
                }
            }
        }
    });

    let connect_config = ConnectConfig {
        name: o.name.clone(),
        device_type: DeviceType::Computer,
        is_group: false,
        initial_volume: mixer.volume(),
        disable_volume: false,
        volume_steps: 64,
    };
    let (spirc, task) = Spirc::new(connect_config, session.clone(), creds, player.clone(), mixer.clone())
        .await
        .map_err(|e| anyhow!("start Connect device: {e}"))?;
    tokio::spawn(task);
    Ok(Link { spirc, player, session })
}

fn is_credentials_error(e: &anyhow::Error) -> bool {
    let s = format!("{e:#}");
    s.contains("INVALID_CREDENTIALS") || s.contains("BadCredentials") || s.contains("Bad credentials")
}

async fn login_needed(tx: &Tx, why: &str) {
    send(tx, json!({"ev": "error", "kind": "login", "msg": why}));
    tokio::time::sleep(Duration::from_millis(100)).await;   // let the writer flush
    std::process::exit(EXIT_LOGIN);
}

fn handle(link: &Link, mixer: &Arc<SoftMixer>, tx: &Tx, cmd: Cmd) {
    let spirc = &link.spirc;
    // Spotify revokes the active role from an idle device, after which librespot drops
    // every command except Activate; it no-ops when we're already active.
    let activate = || {
        let _ = spirc.activate();
    };
    let (name, result) = match cmd {
        Cmd::PlayContext { uri, track_uri, index, position_ms, shuffle } => {
            activate();
            let options = LoadRequestOptions {
                start_playing: true,
                seek_to: position_ms,
                context_options: shuffle.then(|| {
                    LoadContextOptions::Options(CtxOptions { shuffle: true, ..Default::default() })
                }),
                playing_track: track_uri.map(PlayingTrack::Uri).or(index.map(PlayingTrack::Index)),
            };
            ("play_context", spirc.load(LoadRequest::from_context_uri(uri, options)))
        }
        Cmd::PlayTracks { uris, start_uri, position_ms } => {
            activate();
            let options = LoadRequestOptions {
                start_playing: true,
                seek_to: position_ms,
                playing_track: start_uri.map(PlayingTrack::Uri),
                ..Default::default()
            };
            ("play_tracks", spirc.load(LoadRequest::from_tracks(uris, options)))
        }
        Cmd::Play => { activate(); ("play", spirc.play()) }
        Cmd::Pause => ("pause", spirc.pause()),
        Cmd::Toggle => { activate(); ("toggle", spirc.play_pause()) }
        Cmd::Next => { activate(); ("next", spirc.next()) }
        Cmd::Prev => { activate(); ("prev", spirc.prev()) }
        Cmd::Seek { ms } => { activate(); ("seek", spirc.set_position_ms(ms)) }
        Cmd::Volume { pct } => {
            let v = from_pct(pct);
            mixer.set_volume(v);   // audible immediately, Connect sync follows
            ("volume", spirc.set_volume(v))
        }
        Cmd::Shuffle { on } => { activate(); ("shuffle", spirc.shuffle(on)) }
        Cmd::Repeat { mode } => {
            activate();
            let r = match mode.as_str() {
                "track" => spirc.repeat_track(true),
                "context" => spirc.repeat_track(false).and_then(|_| spirc.repeat(true)),
                _ => spirc.repeat_track(false).and_then(|_| spirc.repeat(false)),
            };
            ("repeat", r)
        }
        Cmd::Activate => ("activate", spirc.activate()),
        Cmd::Token { id } => {
            let session = link.session.clone();
            let tx = tx.clone();
            tokio::spawn(async move {
                let got = tokio::time::timeout(Duration::from_secs(10), session.login5().auth_token()).await;
                let v = match got {
                    Ok(Ok(t)) => json!({
                        "ev": "token", "id": id, "access_token": t.access_token,
                        "expires_in": t.expires_in.as_secs(), "scopes": t.scopes,
                    }),
                    Ok(Err(e)) => json!({"ev": "token", "id": id, "error": e.to_string()}),
                    Err(_) => json!({"ev": "token", "id": id, "error": "timed out"}),
                };
                let _ = tx.send(v);
            });
            return;
        }
        Cmd::Quit => return,
    };
    if let Err(e) = result {
        let msg = if link.session.is_invalid() { "reconnecting to Spotify".to_string() } else { e.to_string() };
        send(tx, json!({"ev": "error", "cmd": name, "msg": msg}));
    }
}

fn event_json(ev: PlayerEvent) -> Option<Value> {
    use PlayerEvent as P;
    let uri = |id: &librespot_core::SpotifyUri| id.to_uri().unwrap_or_default();
    Some(match ev {
        P::TrackChanged { audio_item: item } => {
            let (artists, album) = match &item.unique_fields {
                UniqueFields::Track { artists, album, .. } => (
                    artists.iter().map(|a| a.name.as_str()).collect::<Vec<_>>().join(", "),
                    album.clone(),
                ),
                UniqueFields::Episode { show_name, .. } => (show_name.clone(), String::new()),
                UniqueFields::Local { artists, album, .. } => {
                    (artists.clone().unwrap_or_default(), album.clone().unwrap_or_default())
                }
            };
            json!({
                "ev": "track", "uri": item.uri, "name": item.name, "artists": artists,
                "album": album, "duration_ms": item.duration_ms,
            })
        }
        P::Playing { track_id, position_ms, .. } => json!({"ev": "playing", "uri": uri(&track_id), "pos": position_ms}),
        P::Paused { track_id, position_ms, .. } => json!({"ev": "paused", "uri": uri(&track_id), "pos": position_ms}),
        P::Loading { track_id, position_ms, .. } => json!({"ev": "loading", "uri": uri(&track_id), "pos": position_ms}),
        P::Seeked { track_id, position_ms, .. } | P::PositionCorrection { track_id, position_ms, .. } => {
            json!({"ev": "pos", "uri": uri(&track_id), "pos": position_ms})
        }
        P::Stopped { .. } => json!({"ev": "stopped"}),
        P::EndOfTrack { track_id, .. } => json!({"ev": "end", "uri": uri(&track_id)}),
        P::Unavailable { track_id, .. } => json!({"ev": "unavailable", "uri": uri(&track_id)}),
        P::VolumeChanged { volume } => json!({"ev": "volume", "pct": to_pct(volume)}),
        P::ShuffleChanged { shuffle } => json!({"ev": "shuffle", "on": shuffle}),
        P::RepeatChanged { context, track } => {
            json!({"ev": "repeat", "mode": if track { "track" } else if context { "context" } else { "off" }})
        }
        P::SessionConnected { .. } => json!({"ev": "active", "on": true}),
        P::SessionDisconnected { .. } => json!({"ev": "active", "on": false}),
        _ => return None,
    })
}

async fn writer(mut rx: UnboundedReceiver<Value>) {
    let stdout = std::io::stdout();
    while let Some(v) = rx.recv().await {
        let mut out = stdout.lock();
        if writeln!(out, "{v}").and_then(|_| out.flush()).is_err() {
            std::process::exit(0);   // SpoTerm closed the pipe
        }
    }
}

fn send(tx: &Tx, v: Value) {
    let _ = tx.send(v);
}

fn to_pct(v: u16) -> u8 {
    ((u32::from(v) * 100 + 32767) / 65535) as u8
}

fn from_pct(p: u8) -> u16 {
    (u32::from(p.min(100)) * 65535 / 100) as u16
}
