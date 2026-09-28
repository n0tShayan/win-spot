//! Spotify Web API calls on SpoTerm's behalf, so the Python UI needs no TLS or HTTP stack.
//!
//! Uses the token SpoTerm's own login wrote (your app's client id, PKCE) and refreshes it
//! in place. Requests run concurrently; each answers with one `api` event.

use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use tokio::sync::Mutex;

const API: &str = "https://api.spotify.com/";
const TOKEN_URL: &str = "https://accounts.spotify.com/api/token";
const TIMEOUT: Duration = Duration::from_secs(10);
const MAX_BODY: usize = 16 << 20;
const REFRESH_MARGIN: f64 = 60.0;
const MAX_WAIT_429: u64 = 5;

/// The token file, in the format SpoTerm's login writes.
#[derive(Clone, Serialize, Deserialize)]
struct Token {
    access_token: String,
    #[serde(default)]
    refresh_token: String,
    #[serde(default)]
    expires_at: f64,
    #[serde(default)]
    scope: String,
    #[serde(default)]
    pkce: bool,
    #[serde(default = "bearer")]
    token_type: String,
}

fn bearer() -> String {
    "Bearer".into()
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

pub struct Web {
    http: reqwest::Client,
    client_id: String,
    path: PathBuf,
    token: Mutex<Option<Token>>,
}

/// A failed call: `status` 0 means it never got an HTTP answer.
pub struct Failure {
    pub status: u16,
    pub msg: String,
    pub body: Value,
    pub retry_after: u64,
}

impl Failure {
    fn net(msg: impl Into<String>) -> Self {
        Failure { status: 0, msg: msg.into(), body: Value::Null, retry_after: 0 }
    }
}

impl Web {
    pub fn new(client_id: String, path: PathBuf) -> anyhow::Result<Self> {
        let http = reqwest::Client::builder()
            .timeout(TIMEOUT)
            .connect_timeout(TIMEOUT)
            .pool_idle_timeout(Duration::from_secs(90))
            .pool_max_idle_per_host(2)
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Web { http, client_id, path, token: Mutex::new(None) })
    }

    /// A valid access token, refreshing it first when it's about to expire or was rejected.
    async fn access(&self, rejected: Option<&str>) -> Result<String, Failure> {
        let mut slot = self.token.lock().await;
        if slot.is_none() || rejected.is_some() {
            // The file may have been rewritten by a fresh login: prefer what's on disk.
            if let Some(disk) = self.read() {
                if Some(disk.access_token.as_str()) != rejected {
                    *slot = Some(disk);
                }
            }
        }
        let tok = slot.clone().ok_or_else(|| login_needed("not signed in"))?;
        let stale = tok.expires_at - now() < REFRESH_MARGIN;
        if !stale && Some(tok.access_token.as_str()) != rejected {
            return Ok(tok.access_token);
        }
        let fresh = self.refresh(&tok).await?;
        let access = fresh.access_token.clone();
        self.write(&fresh);
        *slot = Some(fresh);
        Ok(access)
    }

    async fn refresh(&self, tok: &Token) -> Result<Token, Failure> {
        if tok.refresh_token.is_empty() {
            return Err(login_needed("sign-in expired"));
        }
        let form = [
            ("grant_type", "refresh_token"),
            ("refresh_token", tok.refresh_token.as_str()),
            ("client_id", self.client_id.as_str()),
        ];
        let resp = self.http.post(TOKEN_URL).form(&form).send().await.map_err(net_failure)?;
        let status = resp.status().as_u16();
        let body = read_json(resp).await?;
        if status != 200 {
            // invalid_grant: the refresh token was revoked or belongs to another client.
            return Err(if status == 400 || status == 401 {
                login_needed("sign-in expired")
            } else {
                Failure { status, msg: format!("token refresh failed ({status})"), body, retry_after: 0 }
            });
        }
        let access = body["access_token"].as_str().unwrap_or_default().to_string();
        if access.is_empty() {
            return Err(Failure::net("token refresh returned no token"));
        }
        Ok(Token {
            access_token: access,
            refresh_token: body["refresh_token"].as_str().map(str::to_string).unwrap_or_else(|| tok.refresh_token.clone()),
            expires_at: now() + body["expires_in"].as_f64().unwrap_or(3600.0),
            scope: body["scope"].as_str().map(str::to_string).unwrap_or_else(|| tok.scope.clone()),
            pkce: tok.pkce,
            token_type: "Bearer".into(),
        })
    }

    fn read(&self) -> Option<Token> {
        let text = std::fs::read_to_string(&self.path).ok()?;
        serde_json::from_str(&text).ok()
    }

    /// Atomic replace, so a crash mid-write can't leave a half-written token.
    fn write(&self, tok: &Token) {
        let tmp = self.path.with_extension("json.tmp");
        let mut v = serde_json::to_value(tok).unwrap_or(Value::Null);
        v["expires_in"] = json!((tok.expires_at - now()).max(0.0) as u64);
        v["expires_at"] = json!(tok.expires_at as u64);
        if std::fs::write(&tmp, v.to_string()).is_ok() {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600));
            }
            let _ = std::fs::rename(&tmp, &self.path);
        }
    }

    /// One Web API call. `path` is relative to /v1/, or a full api.spotify.com URL (paging).
    pub async fn call(
        &self,
        method: &str,
        path: &str,
        query: Option<Map<String, Value>>,
        body: Option<Value>,
    ) -> Result<(u16, Value), Failure> {
        let url = if path.starts_with("https://") {
            if !path.starts_with(API) {
                return Err(Failure::net("refusing a link to another host"));
            }
            path.to_string()
        } else {
            format!("{API}v1/{}", path.trim_start_matches('/'))
        };
        let method = reqwest::Method::from_bytes(method.as_bytes()).map_err(|_| Failure::net("bad method"))?;
        let idempotent = method != reqwest::Method::POST;
        let query: Vec<(String, String)> = query
            .unwrap_or_default()
            .into_iter()
            .filter(|(_, v)| !v.is_null())
            .map(|(k, v)| (k, v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())))
            .collect();

        let mut rejected: Option<String> = None;
        let mut retried = false;
        loop {
            let token = self.access(rejected.as_deref()).await?;
            let mut req = self.http.request(method.clone(), &url).bearer_auth(&token).query(&query);
            req = match &body {
                Some(b) => req
                    .header(reqwest::header::CONTENT_TYPE, "application/json")
                    .body(serde_json::to_vec(b).unwrap_or_default()),
                None if method == reqwest::Method::PUT || method == reqwest::Method::POST => {
                    req.header(reqwest::header::CONTENT_LENGTH, "0")
                }
                None => req,
            };
            let resp = match req.send().await {
                Ok(r) => r,
                Err(e) if idempotent && !retried => {
                    retried = true;
                    log::warn!("retrying after network error: {e}");
                    tokio::time::sleep(Duration::from_millis(500)).await;
                    continue;
                }
                Err(e) => return Err(net_failure(e)),
            };
            let status = resp.status().as_u16();
            if status == 401 && rejected.is_none() {
                rejected = Some(token); // expired or revoked early: refresh once and retry
                continue;
            }
            let retry_after = resp
                .headers()
                .get(reqwest::header::RETRY_AFTER)
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.trim().parse::<u64>().ok())
                .unwrap_or(0);
            if status == 429 && retry_after <= MAX_WAIT_429 && !retried {
                retried = true;
                tokio::time::sleep(Duration::from_secs(retry_after.max(1))).await;
                continue;
            }
            if status >= 500 && idempotent && !retried {
                retried = true;
                tokio::time::sleep(Duration::from_millis(500)).await;
                continue;
            }
            let value = read_json(resp).await?;
            if status >= 400 {
                let msg = value["error"]["message"]
                    .as_str()
                    .or_else(|| value["error_description"].as_str())
                    .or_else(|| value["error"].as_str())
                    .unwrap_or("request failed")
                    .to_string();
                return Err(Failure { status, msg, body: value, retry_after });
            }
            return Ok((status, value));
        }
    }
}

fn login_needed(msg: &str) -> Failure {
    Failure { status: 401, msg: msg.into(), body: json!({"login": true}), retry_after: 0 }
}

fn net_failure(e: reqwest::Error) -> Failure {
    Failure::net(if e.is_timeout() {
        "Spotify took too long to respond".to_string()
    } else if e.is_connect() {
        "Network error: can't reach Spotify".to_string()
    } else {
        format!("Network error: {}", e.without_url())
    })
}

/// The body as JSON (empty is null), refusing anything over MAX_BODY.
async fn read_json(mut resp: reqwest::Response) -> Result<Value, Failure> {
    if resp.content_length().is_some_and(|n| n as usize > MAX_BODY) {
        return Err(Failure::net("response too large"));
    }
    let mut buf = Vec::new();
    while let Some(chunk) = resp.chunk().await.map_err(net_failure)? {
        if buf.len() + chunk.len() > MAX_BODY {
            return Err(Failure::net("response too large"));
        }
        buf.extend_from_slice(&chunk);
    }
    if buf.iter().all(u8::is_ascii_whitespace) {
        return Ok(Value::Null);
    }
    serde_json::from_slice(&buf).map_err(|_| Failure::net("Spotify sent a malformed response"))
}
