//! spoterm-engine: SpoTerm's built-in Spotify Connect player.
//!
//! `spoterm-engine login --cache DIR` runs the one-time browser login and stores
//! reusable credentials in DIR.
//!
//! `spoterm-engine run --cache DIR --client-id ID --token FILE [--name NAME]
//! [--bitrate 96|160|320] [--volume PCT] [--no-player]` speaks JSON lines: commands on
//! stdin, events on stdout, logs on stderr. It does two jobs for the Python UI:
//!
//! * plays audio as a Spotify Connect device; playback commands go straight to the
//!   local player, so they act instantly with no Web API round trip;
//! * makes Web API calls (`api` commands) with SpoTerm's own token, so the UI needs no
//!   TLS or HTTP code of its own.
//!
//! Closing stdin shuts everything down, so it never outlives SpoTerm.
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
use librespot_playback::audio_backend::{self, Sink, SinkBuilder, SinkError, SinkResult};
use librespot_playback::convert::Converter;
use librespot_playback::decoder::AudioPacket;
use librespot_playback::config::{AudioFormat, Bitrate, PlayerConfig};
use librespot_playback::mixer::softmixer::SoftMixer;
use librespot_playback::mixer::{Mixer, MixerConfig};
use librespot_playback::player::{Player, PlayerEvent};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};

mod web;

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
const HEALTH_CHECK: Duration = Duration::from_secs(5);
const RETRY_MIN: Duration = Duration::from_secs(5);
const RETRY_MAX: Duration = Duration::from_secs(120);

type Tx = UnboundedSender<Value>;

struct Opts {
    cache: PathBuf,
    name: String,
    bitrate: Bitrate,
    volume_pct: u8,
    client_id: String,
    token: PathBuf,
    player: bool,
}

fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("warn"))
        .target(env_logger::Target::Stderr)
        .init();
    let result = parse_args().and_then(|(mode, opts)| match mode.as_str() {
        "login" => login(&opts),
        "run" => run(opts),
        _ => bail!("usage: spoterm-engine login|run --cache DIR [--client-id ID --token FILE] \
                    [--name N] [--bitrate 96|160|320] [--volume PCT] [--no-player]"),
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
        client_id: String::new(),
        token: PathBuf::new(),
        player: true,
    };
    while let Some(flag) = args.next() {
        if flag == "--no-player" {
            opts.player = false;
            continue;
        }
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
            "--client-id" => opts.client_id = value,
            "--token" => opts.token = PathBuf::from(value),
            _ => bail!("unknown option {flag}"),
        }
    }
    if opts.cache.as_os_str().is_empty() {
        bail!("--cache is required");
    }
    Ok((mode, opts))
}

fn runtime() -> Result<tokio::runtime::Runtime> {
    // One async thread is plenty (the player decodes on its own thread), and a small
    // blocking pool keeps the thread count, and so memory, down.
    tokio::runtime::Builder::new_current_thread()
        .max_blocking_threads(4)
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
    Api {
        id: u64,
        #[serde(default = "get")]
        method: String,
        path: String,
        #[serde(default)]
        query: Option<serde_json::Map<String, Value>>,
        #[serde(default)]
        body: Option<Value>,
    },
    Quit,
}

fn get() -> String {
    "GET".into()
}

fn run(o: Opts) -> Result<()> {
    runtime()?.block_on(async move {
        let (tx, rx) = unbounded_channel();
        tokio::spawn(writer(rx));
        let web = Arc::new(web::Web::new(o.client_id.clone(), o.token.clone())?);
        send(&tx, json!({"ev": "hello"}));

        let cache = open_cache(&o.cache)?;
        let mixer = Arc::new(SoftMixer::open(MixerConfig::default()).map_err(|e| anyhow!("mixer: {e}"))?);
        mixer.set_volume(cache.volume().unwrap_or(from_pct(o.volume_pct)));
        let signed_in = o.player && cache.credentials().is_some();
        drop(cache);
        if o.player && !signed_in {
            send(&tx, json!({"ev": "player", "state": "login"}));
        }

        // The player connects in the background (a few seconds), so Web API calls are
        // served from the first moment.
        let mut link: Option<Link> = None;
        let mut connecting = signed_in.then(|| Box::pin(connect(&o, &mixer, &tx)));
        let mut lines = BufReader::new(tokio::io::stdin()).lines();
        let mut health = tokio::time::interval(HEALTH_CHECK);
        health.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let (mut backoff, mut retry_at) = (RETRY_MIN, Instant::now());
        let mut player_ok = signed_in;

        loop {
            tokio::select! {
                res = async { connecting.as_mut().unwrap().await }, if connecting.is_some() => {
                    connecting = None;
                    match res {
                        Ok(fresh) => {
                            let reconnect = link.is_some();
                            if let Some(dead) = link.replace(fresh) {
                                let _ = dead.spirc.shutdown();
                            }
                            let dev = link.as_ref().map(|l| l.session.device_id().to_string());
                            backoff = RETRY_MIN;
                            send(&tx, if reconnect {
                                json!({"ev": "reconnected", "device_id": dev})
                            } else {
                                json!({"ev": "ready", "device_id": dev, "name": o.name, "volume": to_pct(mixer.volume())})
                            });
                        }
                        Err(e) if is_credentials_error(&e) => {
                            let _ = std::fs::remove_file(o.cache.join("credentials.json"));
                            player_ok = false;
                            send(&tx, json!({"ev": "player", "state": "login"}));
                        }
                        Err(e) => {
                            log::warn!("player connect failed: {e:#}");
                            send(&tx, json!({"ev": "player", "state": "retrying", "msg": format!("{e:#}")}));
                            retry_at = Instant::now() + backoff;
                            backoff = (backoff * 2).min(RETRY_MAX);
                        }
                    }
                }
                line = lines.next_line() => {
                    let Ok(Some(line)) = line else { break };   // EOF: SpoTerm has gone
                    if line.trim().is_empty() {
                        continue;
                    }
                    match serde_json::from_str::<Cmd>(&line) {
                        Ok(Cmd::Quit) => break,
                        Ok(Cmd::Api { id, method, path, query, body }) => {
                            let (web, tx) = (web.clone(), tx.clone());
                            tokio::spawn(async move {
                                let v = match web.call(&method, &path, query, body).await {
                                    Ok((status, body)) => json!({"ev": "api", "id": id, "status": status, "body": body}),
                                    Err(f) => json!({"ev": "api", "id": id, "status": f.status, "error": f.msg,
                                                     "body": f.body, "retry_after": f.retry_after}),
                                };
                                let _ = tx.send(v);
                            });
                        }
                        Ok(cmd) => handle(link.as_ref(), &mixer, &tx, cmd),
                        Err(e) => send(&tx, json!({"ev": "error", "cmd": "parse", "msg": e.to_string()})),
                    }
                }
                _ = health.tick(), if player_ok => {
                    // librespot invalidates the session when a keep-alive goes unanswered and
                    // leaves recovery to us, so rebuild it with backoff.
                    let dead = link.as_ref().is_none_or(|l| l.session.is_invalid());
                    if !dead || connecting.is_some() || Instant::now() < retry_at {
                        continue;
                    }
                    if link.is_some() {
                        send(&tx, json!({"ev": "reconnecting"}));
                    }
                    connecting = Some(Box::pin(connect(&o, &mixer, &tx)));
                }
            }
        }
        drop(connecting);
        if let Some(link) = link {
            let _ = link.spirc.shutdown();
            link.player.stop();
            tokio::time::sleep(Duration::from_millis(200)).await;   // let the device say goodbye
        }
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
    let sink_tx = tx.clone();
    let player = Player::new(config, session.clone(), mixer.get_soft_volume(), move || {
        Box::new(LazySink { backend, sink: None, tx: sink_tx }) as Box<dyn Sink>
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

/// Opens the sound device only while audio plays. rodio keeps its output stream, and the
/// OS audio callback thread behind it, running for as long as the sink exists, which costs
/// CPU even when paused; so the real sink is dropped whenever librespot stops it. Opening
/// also can't take the engine down: rodio panics when there is no output device, and that
/// becomes a normal error (librespot then pauses) plus a message for the UI.
struct LazySink {
    backend: SinkBuilder,
    sink: Option<Box<dyn Sink>>,
    tx: Tx,
}

impl LazySink {
    fn open(&mut self) -> SinkResult<&mut Box<dyn Sink>> {
        if self.sink.is_none() {
            let backend = self.backend;
            let opened = std::panic::catch_unwind(|| backend(None, AudioFormat::default()));
            match opened {
                Ok(sink) => self.sink = Some(sink),
                Err(_) => {
                    send(&self.tx, json!({"ev": "error", "cmd": "audio", "msg": "no audio output device available"}));
                    return Err(SinkError::ConnectionRefused("no audio output device".into()));
                }
            }
        }
        Ok(self.sink.as_mut().expect("just opened"))
    }
}

impl Sink for LazySink {
    fn start(&mut self) -> SinkResult<()> {
        self.open()?.start()
    }

    fn stop(&mut self) -> SinkResult<()> {
        // librespot exits the process if stop fails, so never report an error here.
        if let Some(mut sink) = self.sink.take() {
            if let Err(e) = sink.stop() {
                log::warn!("audio stop: {e}");
            }
        }
        Ok(())
    }

    fn write(&mut self, packet: AudioPacket, converter: &mut Converter) -> SinkResult<()> {
        self.open()?.write(packet, converter)
    }
}

fn is_credentials_error(e: &anyhow::Error) -> bool {
    let s = format!("{e:#}");
    s.contains("INVALID_CREDENTIALS") || s.contains("BadCredentials") || s.contains("Bad credentials")
}

fn handle(link: Option<&Link>, mixer: &Arc<SoftMixer>, tx: &Tx, cmd: Cmd) {
    let Some(link) = link else {
        send(tx, json!({"ev": "error", "cmd": "player", "msg": "SpoTerm's player isn't connected yet"}));
        return;
    };
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
        Cmd::Api { .. } => return,
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
