use serde::Deserialize;

/// OpenClaw CLI `--json` 返回的顶层结构
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CliResponse {
    pub run_id: String,
    pub status: String,
    pub summary: String,
    pub result: CliResult,
}

/// CLI 返回的 result 字段
#[derive(Debug, Deserialize)]
pub struct CliResult {
    pub payloads: Vec<Payload>,
    pub meta: CliMeta,
}

/// 单个 payload 条目
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Payload {
    pub text: Option<String>,
    pub media_url: Option<String>,
}

/// CLI 返回的 meta 信息
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CliMeta {
    pub duration_ms: u64,
    pub agent_meta: AgentMeta,
    pub error: Option<CliError>,
}

/// Agent 元数据
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentMeta {
    pub session_id: String,
    pub provider: String,
    pub model: String,
}

/// 错误信息
#[derive(Debug, Deserialize)]
pub struct CliError {
    pub kind: String,
    pub message: String,
}
