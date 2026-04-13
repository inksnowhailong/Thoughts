use chrono::{DateTime, Local};
use serde::{Deserialize, Serialize};

// === 运行时状态 ===

/// Gateway 运行状态
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum GatewayStatus {
    Stopped,
    Starting,
    Ready,
    Error(String),
}

/// 自动轮询调度器状态
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum SchedulerStatus {
    Idle,
    Running,
    Retrying,
    Paused,
}

// === 应用配置 ===

/// 静默时间段配置
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuietHours {
    pub enabled: bool,
    pub start: String,
    pub end: String,
}

/// 应用配置，对应本地 JSON 配置文件
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AppConfig {
    pub openclaw_binary_path: String,
    pub gateway_port: u16,
    pub profile: String,
    pub agent_id: String,
    pub user_session_key: String,
    pub auto_session_key: String,
    pub auto_poll_enabled: bool,
    pub auto_poll_minutes: u64,
    pub quiet_hours: QuietHours,
    pub auto_topics: Vec<String>,
    pub request_timeout_seconds: u64,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            openclaw_binary_path: "openclaw".to_string(),
            gateway_port: 18789,
            profile: "thoughts".to_string(),
            agent_id: "main".to_string(),
            user_session_key: "thoughts-user-main".to_string(),
            auto_session_key: "thoughts-auto-main".to_string(),
            auto_poll_enabled: true,
            auto_poll_minutes: 10,
            quiet_hours: QuietHours {
                enabled: false,
                start: "23:00".to_string(),
                end: "08:00".to_string(),
            },
            auto_topics: vec![
                "天气".to_string(),
                "节日".to_string(),
                "新闻大事".to_string(),
                "设备状态".to_string(),
                "用户可能感兴趣的内容".to_string(),
            ],
            request_timeout_seconds: 45,
        }
    }
}

// === 事件日志 ===

/// 单条事件日志记录，对应 JSONL 中的一行
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct EventLog {
    pub event_id: String,
    pub trigger: String,
    pub session_key: String,
    pub sent_at: DateTime<Local>,
    pub replied_at: Option<DateTime<Local>>,
    pub sent_content: String,
    pub received_content: Option<String>,
    pub status: String,
    pub result_kind: Option<String>,
}

// === 前端通信 ===

/// 传给前端的状态快照
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AppStatus {
    pub gateway: GatewayStatus,
    pub scheduler: SchedulerStatus,
    pub last_error: Option<String>,
    pub last_poll_time: Option<String>,
    pub last_user_message_time: Option<String>,
}
