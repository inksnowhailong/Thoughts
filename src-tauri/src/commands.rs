use crate::app::state::SharedState;
use crate::cli::agent_client;
use crate::models::{AppStatus, EventLog, GatewayStatus};
use crate::runtime::{openclaw_manager, scheduler};
use crate::storage::event_log_store;
use chrono::Local;
use std::path::PathBuf;
use tauri::Manager;

/// 构建用户消息文本模板
fn build_user_template(content: &str) -> String {
    let now = Local::now().format("%Y-%m-%dT%H:%M:%S%:z");
    format!(
        "[Thoughts Event]\n\
         type: user_message\n\
         time: {}\n\
         source: ui\n\n\
         [User Message]\n\
         {}\n\n\
         [Instructions]\n\
         - 直接回复用户。\n\
         - 如果需要额外权限才能行动，明确说明。",
        now, content
    )
}

/// 获取应用数据目录
fn get_app_data_dir(app_handle: &tauri::AppHandle) -> PathBuf {
    app_handle
        .path()
        .app_data_dir()
        .unwrap_or_else(|_| PathBuf::from("."))
}

/// 发送用户消息给 OpenClaw 并返回 AI 回复
#[tauri::command]
pub async fn send_user_message(
    message: String,
    state: tauri::State<'_, SharedState>,
    app_handle: tauri::AppHandle,
) -> Result<String, String> {
    // 标记用户正在对话
    {
        let mut s = state.write().await;
        s.user_is_chatting = true;
        s.last_user_message_time = Some(Local::now());
    }

    let (binary_path, session_key, timeout) = {
        let s = state.read().await;
        (
            s.config.openclaw_binary_path.clone(),
            s.config.user_session_key.clone(),
            s.config.request_timeout_seconds,
        )
    };

    let app_data_dir = get_app_data_dir(&app_handle);
    let template = build_user_template(&message);
    let sent_at = Local::now();
    let event_id = event_log_store::generate_event_id("user_message");

    let result =
        agent_client::send_to_openclaw(&binary_path, &template, &session_key, timeout).await;

    // 标记对话结束，重置轮询计时
    {
        let mut s = state.write().await;
        s.user_is_chatting = false;
    }

    match result {
        Ok(response) => {
            let text = agent_client::extract_text(&response)?;

            event_log_store::append_event(
                &app_data_dir,
                &EventLog {
                    event_id,
                    trigger: "user_message".to_string(),
                    session_key,
                    sent_at,
                    replied_at: Some(Local::now()),
                    sent_content: message,
                    received_content: Some(text.clone()),
                    status: "success".to_string(),
                    result_kind: Some("normal".to_string()),
                },
            );

            Ok(text)
        }
        Err(e) => {
            event_log_store::append_event(
                &app_data_dir,
                &EventLog {
                    event_id,
                    trigger: "user_message".to_string(),
                    session_key,
                    sent_at,
                    replied_at: None,
                    sent_content: message,
                    received_content: None,
                    status: "error".to_string(),
                    result_kind: Some("error".to_string()),
                },
            );
            Err(e)
        }
    }
}

/// 获取当前应用状态
#[tauri::command]
pub async fn get_status(state: tauri::State<'_, SharedState>) -> Result<AppStatus, String> {
    let s = state.read().await;
    Ok(s.to_status())
}

/// 手动触发一次自动检查
#[tauri::command]
pub async fn trigger_manual_poll(
    state: tauri::State<'_, SharedState>,
    app_handle: tauri::AppHandle,
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

    let app_data_dir = get_app_data_dir(&app_handle);
    let template = scheduler::build_poll_template(&topics);
    let sent_at = Local::now();
    let event_id = event_log_store::generate_event_id("manual_poll");

    let result =
        agent_client::send_to_openclaw(&binary_path, &template, &session_key, timeout).await;

    match result {
        Ok(response) => {
            let text = agent_client::extract_text(&response)?;

            let is_heartbeat = text.contains("HEARTBEAT_OK");
            event_log_store::append_event(
                &app_data_dir,
                &EventLog {
                    event_id,
                    trigger: "manual_poll".to_string(),
                    session_key,
                    sent_at,
                    replied_at: Some(Local::now()),
                    sent_content: "手动触发检查".to_string(),
                    received_content: Some(text.clone()),
                    status: "success".to_string(),
                    result_kind: Some(if is_heartbeat { "heartbeat_ok" } else { "normal" }.to_string()),
                },
            );

            if is_heartbeat { Ok(None) } else { Ok(Some(text)) }
        }
        Err(e) => {
            event_log_store::append_event(
                &app_data_dir,
                &EventLog {
                    event_id,
                    trigger: "manual_poll".to_string(),
                    session_key,
                    sent_at,
                    replied_at: None,
                    sent_content: "手动触发检查".to_string(),
                    received_content: None,
                    status: "error".to_string(),
                    result_kind: Some("error".to_string()),
                },
            );
            Err(e)
        }
    }
}

/// 重启 Gateway
#[tauri::command]
pub async fn restart_gateway(state: tauri::State<'_, SharedState>) -> Result<(), String> {
    let binary_path = {
        let s = state.read().await;
        s.config.openclaw_binary_path.clone()
    };

    {
        let mut s = state.write().await;
        s.gateway_status = GatewayStatus::Starting;
    }

    let _ = openclaw_manager::stop_gateway(&binary_path).await;

    match openclaw_manager::ensure_gateway_running(&binary_path).await {
        Ok(()) => {
            let mut s = state.write().await;
            s.gateway_status = GatewayStatus::Ready;
            s.last_error = None;
            Ok(())
        }
        Err(e) => {
            let mut s = state.write().await;
            s.gateway_status = GatewayStatus::Error(e.clone());
            s.last_error = Some(e.clone());
            Err(e)
        }
    }
}
