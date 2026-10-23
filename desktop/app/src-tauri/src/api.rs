//! ContextShrink control-plane API (the same endpoints web dashboard uses).
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

/// Public dashboard origin. `CONTEXTSHRINK_APP_URL` overrides it for testing.
pub fn app_url() -> String {
    std::env::var("CONTEXTSHRINK_APP_URL")
        .unwrap_or_else(|_| "https://app.contextshrink.com".into())
        .trim_end_matches('/')
        .to_string()
}

/// Control-plane API. `CONTEXTSHRINK_API_URL` overrides it for testing.
fn api_url(path: &str) -> String {
    let base = std::env::var("CONTEXTSHRINK_API_URL")
        .unwrap_or_else(|_| "https://api.contextshrink.com".into());
    format!("{}{}", base.trim_end_matches('/'), path)
}

/// Hosted proxy local forwarder relays to a separate origin.
pub fn proxy_url() -> String {
    std::env::var("CONTEXTSHRINK_PROXY_URL")
        .unwrap_or_else(|_| "https://proxy.contextshrink.com".into())
        .trim_end_matches('/')
        .to_string()
}

#[derive(Debug)]
pub enum ApiError {
    Unauthorized,
    Message(String),
}
impl ApiError {
    pub fn text(&self) -> String {
        match self {
            ApiError::Unauthorized => "Your session expired. Please sign in again.".into(),
            ApiError::Message(message) => message.clone(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct User {
    pub id: String,
    pub name: String,
    pub email: String,
    #[serde(default)]
    pub plan: Option<String>,
    #[serde(default)]
    pub subscription_status: Option<String>,
    #[serde(default)]
    pub team_id: Option<String>,
    #[serde(default)]
    pub team_role: Option<String>,
    #[serde(default)]
    pub personal_plan: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct DeviceKey {
    pub id: String,
    pub key: String,
}

pub struct Api {
    http: reqwest::Client,
}
impl Api {
    pub fn new() -> Self {
        let http = reqwest::Client::builder()
            .user_agent(concat!("ContextShrinkDesktop/", env!("CARGO_PKG_VERSION")))
            .timeout(std::time::Duration::from_secs(20))
            .build()
            .expect("HTTP client");
        Api { http }
    }

    fn scoped(req: reqwest::RequestBuilder, team_id: Option<&str>) -> reqwest::RequestBuilder {
        match team_id.filter(|id| !id.trim().is_empty()) {
            Some(id) => req.header("X-ContextShrink-Team", id.trim()),
            None => req,
        }
    }

    async fn send(&self, req: reqwest::RequestBuilder) -> Result<Value, ApiError> {
        let resp = req.send().await.map_err(|_| {
            ApiError::Message("Cannot reach ContextShrink. Check your internet connection.".into())
        })?;
        let status = resp.status();
        if status == reqwest::StatusCode::UNAUTHORIZED {
            return Err(ApiError::Unauthorized);
        }
        let body: Value = resp.json().await.unwrap_or(Value::Null);
        if !status.is_success() {
            let detail = body
                .get("detail")
                .and_then(Value::as_str)
                .map(str::to_string)
                .unwrap_or_else(|| format!("Request failed ({status})"));
            return Err(ApiError::Message(detail));
        }
        Ok(body)
    }

    /// Login is always personal/unscoped. Workspace context is applied after authentication.
    pub async fn login(&self, email: &str, password: &str) -> Result<(String, User), ApiError> {
        let body = self
            .send(
                self.http
                    .post(api_url("/auth/login"))
                    .json(&json!({ "email": email, "password": password })),
            )
            .await
            .map_err(|error| match error {
                ApiError::Unauthorized => ApiError::Message("Incorrect email or password.".into()),
                other => other,
            })?;
        let token = body["token"].as_str().unwrap_or_default().to_string();
        let user = serde_json::from_value(body["user"].clone())
            .map_err(|_| ApiError::Message("Unexpected login response".into()))?;
        Ok((token, user))
    }

    pub async fn me(&self, token: &str, team_id: Option<&str>) -> Result<User, ApiError> {
        let req = Self::scoped(
            self.http.get(api_url("/auth/me")).bearer_auth(token),
            team_id,
        );
        let body = self.send(req).await?;
        serde_json::from_value(body)
            .map_err(|_| ApiError::Message("Unexpected account response".into()))
    }

    /// Plan, savings cycle, Free cap and any unpaid fee.
    pub async fn billing_estimate(
        &self,
        token: &str,
        team_id: Option<&str>,
    ) -> Result<Value, ApiError> {
        self.send(Self::scoped(
            self.http
                .get(api_url("/billing/estimate"))
                .bearer_auth(token),
            team_id,
        ))
        .await
    }

    /// Lists this user's keys in the selected workspace.
    pub async fn list_keys(&self, token: &str, team_id: Option<&str>) -> Result<Value, ApiError> {
        self.send(Self::scoped(
            self.http.get(api_url("/keys")).bearer_auth(token),
            team_id,
        ))
        .await
    }

    /// Creates a proxy key for this device in the selected workspace.
    pub async fn create_device_key(
        &self,
        token: &str,
        name: &str,
        team_id: Option<&str>,
    ) -> Result<DeviceKey, ApiError> {
        let req = Self::scoped(
            self.http
                .post(api_url("/keys"))
                .bearer_auth(token)
                .json(&json!({ "name": name, "scopes": ["proxy:messages", "proxy:responses"] })),
            team_id,
        );
        let body = self.send(req).await?;
        serde_json::from_value(body)
            .map_err(|_| ApiError::Message("Unexpected key response".into()))
    }

    pub async fn key_is_active(
        &self,
        token: &str,
        key_id: &str,
        team_id: Option<&str>,
    ) -> Result<bool, ApiError> {
        let body = self.list_keys(token, team_id).await?;
        Ok(body.as_array().is_some_and(|keys| {
            keys.iter()
                .any(|key| key["id"].as_str() == Some(key_id) && key["revoked_at"].is_null())
        }))
    }

    pub async fn revoke_key(
        &self,
        token: &str,
        key_id: &str,
        team_id: Option<&str>,
    ) -> Result<(), ApiError> {
        let req = Self::scoped(
            self.http
                .delete(api_url(&format!("/keys/{key_id}")))
                .bearer_auth(token),
            team_id,
        );
        self.send(req).await.map(|_| ())
    }

    /// Logout has no workspace context so it cannot be blocked by stale team access.
    pub async fn logout(&self, token: &str) {
        let _ = self
            .http
            .post(api_url("/auth/logout"))
            .bearer_auth(token)
            .send()
            .await;
    }
}
