use crate::app::state::SharedState;
use crate::cli::agent_client;
use crate::models::{EventLog, GatewayStatus, SchedulerStatus};
use crate::storage::event_log_store;
use chrono::Local;
use std::path::PathBuf;
use std::time::Duration;
use tauri::Emitter;

/// 构建自动轮询文本模板（供手动触发复用）
pub fn build_poll_template(topics: &[String]) -> String {
    let now = Local::now().format("%Y-%m-%dT%H:%M:%S%:z");
    let topics_str = topics
        .iter()
        .map(|t| format!("- {}", t))
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        "[Thoughts Event]\n\
         type: auto_poll\n\
         time: {}\n\
         source: scheduler\n\
         topics:\n\
         {}\n\n\
         [Instructions]\n\
         - 请根据当前时间和上下文，自行判断本轮值得关注的信息。\n\
         - 如果没有值得提醒用户的内容，回复 HEARTBEAT_OK。\n\
         - 如果有值得告诉用户的内容，生成一段简短自然的中文提醒。\n\
         - 不执行高风险动作。",
        now, topics_str
    )
}

/// 执行一次自动轮询
async fn execute_poll(
    state: &SharedState,
    app_data_dir: &PathBuf,
) -> Result<Option<String>, String> {
    let (binary_path, session_key, timeout, topics) = {
        let s = state.read().await;
        (
            s.config.openclaw_binary_path.clone(),
            s.config.auto_session_key.clone(),
            s.config.request_timeout_seconds,
            s.config.auto_topics.clone(),
        )
    };

    let template = build_poll_template(&topics);
    let sent_at = Local::now();
    let event_id = event_log_store::generate_event_id("auto_poll");

    let result = agent_client::send_to_openclaw(&binary_path, &template, &session_key, timeout).await;

    match result {
        Ok(response) => {
            let text = agent_client::extract_text(&response);
            let replied_at = Local::now();

            // 记录日志
            let (status, result_kind, received) = match &text {
                Ok(t) if t.lines().any(|l| l.trim() == "HEARTBEAT_OK") => {
                    ("success".to_string(), Some("heartbeat_ok".to_string()), Some(t.clone()))
                }
                Ok(t) => ("success".to_string(), Some("normal".to_string()), Some(t.clone())),
                Err(e) => ("error".to_string(), Some("error".to_string()), Some(e.clone())),
            };

            event_log_store::append_event(
                app_data_dir,
                &EventLog {
                    event_id,
                    trigger: "auto_poll".to_string(),
                    session_key,
                    sent_at,
                    replied_at: Some(replied_at),
                    sent_content: "自动轮询".to_string(),
                    received_content: received.clone(),
                    status,
                    result_kind,
                },
            );

            match text {
                Ok(t) if t.lines().any(|l| l.trim() == "HEARTBEAT_OK") => Ok(None),
                Ok(t) => Ok(Some(t)),
                Err(e) => Err(e),
            }
        }
        Err(e) => {
            event_log_store::append_event(
                app_data_dir,
                &EventLog {
                    event_id,
                    trigger: "auto_poll".to_string(),
                    session_key,
                    sent_at,
                    replied_at: None,
                    sent_content: "自动轮询".to_string(),
                    received_content: None,
                    status: "error".to_string(),
                    result_kind: Some("error".to_string()),
                },
            );
            Err(e)
        }
    }
}

/// 调度器主循环
pub async fn run_scheduler(
    state: SharedState,
    app_handle: tauri::AppHandle,
    app_data_dir: PathBuf,
    mut stop_rx: tokio::sync::watch::Receiver<bool>,
) {
    loop {
        let poll_minutes = {
            let s = state.read().await;
            s.config.auto_poll_minutes
        };

        // 等待轮询间隔或停止信号
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(poll_minutes * 60)) => {},
            _ = stop_rx.changed() => {
                if *stop_rx.borrow() {
                    break;
                }
                continue;
            }
        }

        // 检查是否应该执行
        let should_run = {
            let s = state.read().await;
            s.config.auto_poll_enabled
                && s.gateway_status == GatewayStatus::Ready
                && !s.user_is_chatting
        };

        if !should_run {
            continue;
        }

        // 更新状态
        {
            let mut s = state.write().await;
            s.scheduler_status = SchedulerStatus::Running;
        }
        let _ = app_handle.emit("status-changed", ());

        // 执行轮询
        match execute_poll(&state, &app_data_dir).await {
            Ok(Some(text)) => {
                // 有内容，推送给前端
                let _ = app_handle.emit("auto-reminder", &text);
                let mut s = state.write().await;
                s.last_poll_time = Some(Local::now());
                s.scheduler_status = SchedulerStatus::Idle;
                s.last_error = None;
            }
            Ok(None) => {
                // HEARTBEAT_OK，安静记录
                let mut s = state.write().await;
                s.last_poll_time = Some(Local::now());
                s.scheduler_status = SchedulerStatus::Idle;
                s.last_error = None;
            }
            Err(_e) => {
                // 重试一次
                {
                    let mut s = state.write().await;
                    s.scheduler_status = SchedulerStatus::Retrying;
                }
                let _ = app_handle.emit("status-changed", ());

                match execute_poll(&state, &app_data_dir).await {
                    Ok(Some(text)) => {
                        let _ = app_handle.emit("auto-reminder", &text);
                        let mut s = state.write().await;
                        s.last_poll_time = Some(Local::now());
                        s.scheduler_status = SchedulerStatus::Idle;
                        s.last_error = None;
                    }
                    Ok(None) => {
                        let mut s = state.write().await;
                        s.last_poll_time = Some(Local::now());
                        s.scheduler_status = SchedulerStatus::Idle;
                        s.last_error = None;
                    }
                    Err(retry_err) => {
                        let mut s = state.write().await;
                        s.scheduler_status = SchedulerStatus::Idle;
                        s.last_error = Some(format!("自动轮询失败(已重试): {}", retry_err));
                    }
                }
            }
        }

        let _ = app_handle.emit("status-changed", ());
    }
}
