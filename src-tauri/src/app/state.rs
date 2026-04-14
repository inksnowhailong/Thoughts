use crate::models::{AppConfig, AppPhase, AppStatus, GatewayStatus, SchedulerStatus};
use chrono::{DateTime, Local};
use std::sync::Arc;
use tokio::sync::RwLock;

/// 全局共享状态类型
pub type SharedState = Arc<RwLock<AppState>>;

/// 应用运行时状态
pub struct AppState {
    pub gateway_status: GatewayStatus,
    pub scheduler_status: SchedulerStatus,
    pub last_error: Option<String>,
    pub last_poll_time: Option<DateTime<Local>>,
    pub last_user_message_time: Option<DateTime<Local>>,
    /// 用户是否正在对话中（自动轮询需要据此暂停）
    pub user_is_chatting: bool,
    pub config: AppConfig,
    /// 应用生命周期阶段
    pub app_phase: AppPhase,
    /// Onboarding 完成信号发送端（仅在 Onboarding 阶段存在）
    pub onboarding_signal: Option<tokio::sync::oneshot::Sender<()>>,
}

impl AppState {
    pub fn new(config: AppConfig, app_phase: AppPhase) -> Self {
        Self {
            gateway_status: GatewayStatus::Stopped,
            scheduler_status: SchedulerStatus::Idle,
            last_error: None,
            last_poll_time: None,
            last_user_message_time: None,
            user_is_chatting: false,
            config,
            app_phase,
            onboarding_signal: None,
        }
    }

    /// 生成传给前端的状态快照
    pub fn to_status(&self) -> AppStatus {
        AppStatus {
            gateway: self.gateway_status.clone(),
            scheduler: self.scheduler_status.clone(),
            last_error: self.last_error.clone(),
            last_poll_time: self.last_poll_time.map(|t| t.format("%H:%M:%S").to_string()),
            last_user_message_time: self
                .last_user_message_time
                .map(|t| t.format("%H:%M:%S").to_string()),
            app_phase: self.app_phase.clone(),
        }
    }
}

/// 创建共享状态实例
pub fn create_shared_state(config: AppConfig, app_phase: AppPhase) -> SharedState {
    Arc::new(RwLock::new(AppState::new(config, app_phase)))
}
