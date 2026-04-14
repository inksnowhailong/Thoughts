use crate::app::state::SharedState;
use crate::cli::agent_client;
use crate::models::{AppPhase, AppStatus, EventLog, GatewayStatus};
use crate::runtime::{openclaw_manager, scheduler};
use crate::storage::{event_log_store, profile_store::ProfileStore};
use chrono::Local;
use serde_json::Value;
use std::path::PathBuf;
use std::sync::Arc;
use tauri::{Emitter, Manager};

// === 标记解析 ===

/// 行级标记检测：标记必须独占一行
fn has_marker(text: &str, marker: &str) -> bool {
    text.lines().any(|l| l.trim() == marker)
}

/// 提取标记后紧跟的 JSON 块
/// 返回 (解析后的 JSON, 清理后的文本) 或 None
fn extract_marker_json(text: &str, marker: &str) -> Option<(Value, String)> {
    let lines: Vec<&str> = text.lines().collect();

    // 查找标记所在行
    let marker_idx = lines.iter().position(|l| l.trim() == marker)?;

    // 取标记行之后的剩余文本
    let remainder: String = lines[marker_idx + 1..].join("\n");

    // 定位第一个 '{'
    let json_start = remainder.find('{')?;
    let json_text = &remainder[json_start..];

    // 花括号计数找到匹配的 '}'
    let mut depth = 0;
    let mut end_idx = 0;
    for (i, ch) in json_text.char_indices() {
        match ch {
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    end_idx = i + 1;
                    break;
                }
            }
            _ => {}
        }
    }

    if end_idx == 0 {
        return None;
    }

    // 尝试解析 JSON
    let json_str = &json_text[..end_idx];
    let value: Value = serde_json::from_str(json_str).ok()?;

    // 构建清理后的文本：移除标记行和 JSON 块
    let mut cleaned_lines: Vec<&str> = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        if i < marker_idx {
            cleaned_lines.push(line);
        }
        // 跳过标记行及其后的 JSON 行
    }
    let cleaned = cleaned_lines.join("\n").trim().to_string();

    Some((value, cleaned))
}

// === 模板构建 ===

/// 构建用户消息文本模板（包含 PROFILE_UPDATE 提示）
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
         - 如果需要额外权限才能行动，明确说明。\n\
         - 如果用户在对话中提到了新的偏好、兴趣变化、或个人信息更新，在回复最后独占一行输出 PROFILE_UPDATE，下一行输出需要更新的字段 JSON（扁平结构，数组字段提供完整值而非增量）。",
        now, content
    )
}

/// 构建 onboarding 消息模板
fn build_onboarding_template(user_message: &str) -> String {
    let now = Local::now().format("%Y-%m-%dT%H:%M:%S%:z");
    format!(
        "[Thoughts Event]\n\
         type: onboarding\n\
         time: {}\n\
         source: ui\n\n\
         [System Instructions]\n\
         你正在进行用户画像收集。这是你和用户的第一次见面。\n\n\
         目标：通过自然、友好的对话，了解用户的基本信息和偏好。\n\
         收集方向（不限于此）：昵称、性别、年龄段、职业、爱好、感兴趣的话题、讨厌的事情、日常习惯等。\n\n\
         对话规则：\n\
         - 每次只问 1-2 个问题，不要一次性列出所有问题\n\
         - 保持轻松自然的语气，像朋友聊天一样\n\
         - 根据用户的回答自然地追问或转向新话题\n\
         - 当你觉得已经对用户有了足够的了解（通常 3-8 轮对话），结束收集\n\n\
         结束方式：\n\
         当你判断信息已充足时，在回复的最后独占一行输出标记，格式严格如下：\n\
         1. 先输出你的告别语/总结语\n\
         2. 然后空一行\n\
         3. 然后独占一行写 ONBOARDING_COMPLETE\n\
         4. 然后下一行写一个扁平的 JSON 对象（不要嵌套对象，数组可以用）\n\n\
         示例格式：\n\
         很高兴认识你！我会记住这些，以后聊天会更有针对性~\n\n\
         ONBOARDING_COMPLETE\n\
         {{\"nickname\": \"小明\", \"gender\": \"男\", \"ageRange\": \"25-30\", \"hobbies\": [\"游戏\", \"编程\"]}}\n\n\
         注意：JSON 必须是单行、合法的 JSON。字段名用 camelCase。\n\n\
         [User Message]\n\
         {}",
        now, user_message
    )
}

/// 获取应用数据目录
fn get_app_data_dir(app_handle: &tauri::AppHandle) -> PathBuf {
    app_handle
        .path()
        .app_data_dir()
        .unwrap_or_else(|_| PathBuf::from("."))
}

// === Tauri Commands ===

/// 发送用户消息给 OpenClaw 并返回 AI 回复（含 PROFILE_UPDATE 检测）
#[tauri::command]
pub async fn send_user_message(
    message: String,
    state: tauri::State<'_, SharedState>,
    profile_store: tauri::State<'_, Arc<ProfileStore>>,
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

    // 标记对话结束
    {
        let mut s = state.write().await;
        s.user_is_chatting = false;
    }

    match result {
        Ok(response) => {
            let text = agent_client::extract_text(&response)?;

            // 检测 PROFILE_UPDATE 标记
            let display_text = if let Some((json, cleaned)) =
                extract_marker_json(&text, "PROFILE_UPDATE")
            {
                // merge 画像并通知前端
                let _ = profile_store.merge(&json).await;
                let _ = app_handle.emit("profile-updated", ());
                if cleaned.is_empty() { text.clone() } else { cleaned }
            } else {
                text.clone()
            };

            event_log_store::append_event(
                &app_data_dir,
                &EventLog {
                    event_id,
                    trigger: "user_message".to_string(),
                    session_key,
                    sent_at,
                    replied_at: Some(Local::now()),
                    sent_content: message,
                    received_content: Some(text),
                    status: "success".to_string(),
                    result_kind: Some("normal".to_string()),
                },
            );

            Ok(display_text)
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

/// 发送 onboarding 消息，检测 ONBOARDING_COMPLETE 标记
#[tauri::command]
pub async fn send_onboarding_message(
    message: String,
    state: tauri::State<'_, SharedState>,
    profile_store: tauri::State<'_, Arc<ProfileStore>>,
    app_handle: tauri::AppHandle,
) -> Result<String, String> {
    let (binary_path, timeout) = {
        let s = state.read().await;
        (
            s.config.openclaw_binary_path.clone(),
            s.config.request_timeout_seconds,
        )
    };

    let template = build_onboarding_template(&message);
    let session_key = "thoughts-onboarding".to_string();

    let result =
        agent_client::send_to_openclaw(&binary_path, &template, &session_key, timeout).await;

    match result {
        Ok(response) => {
            let text = agent_client::extract_text(&response)?;

            // 检测 ONBOARDING_COMPLETE 标记
            if let Some((json, cleaned)) = extract_marker_json(&text, "ONBOARDING_COMPLETE") {
                // 保存画像
                profile_store.save(&json).await?;

                // 切换 AppPhase 并发送 onboarding 完成信号
                let signal = {
                    let mut s = state.write().await;
                    s.app_phase = AppPhase::Active;
                    s.onboarding_signal.take()
                };
                if let Some(tx) = signal {
                    let _ = tx.send(());
                }

                // 通知前端
                let _ = app_handle.emit("onboarding-complete", ());

                // 返回清理后的文本（去除标记和 JSON）
                let display = if cleaned.is_empty() {
                    "画像收集完成！".to_string()
                } else {
                    cleaned
                };
                Ok(display)
            } else {
                // 未完成，正常返回对话文本
                Ok(text)
            }
        }
        Err(e) => Err(e),
    }
}

/// 跳过 onboarding，创建空画像
#[tauri::command]
pub async fn skip_onboarding(
    state: tauri::State<'_, SharedState>,
    profile_store: tauri::State<'_, Arc<ProfileStore>>,
    app_handle: tauri::AppHandle,
) -> Result<(), String> {
    // 保存空画像
    let empty = serde_json::json!({});
    profile_store.save(&empty).await?;

    // 切换 AppPhase 并发送信号
    let signal = {
        let mut s = state.write().await;
        s.app_phase = AppPhase::Active;
        s.onboarding_signal.take()
    };
    if let Some(tx) = signal {
        let _ = tx.send(());
    }

    let _ = app_handle.emit("onboarding-complete", ());
    Ok(())
}

/// 获取当前用户画像
#[tauri::command]
pub async fn get_user_profile(
    profile_store: tauri::State<'_, Arc<ProfileStore>>,
) -> Result<Option<Value>, String> {
    Ok(profile_store.load().await)
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

            let is_heartbeat = has_marker(&text, "HEARTBEAT_OK");
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
                    result_kind: Some(
                        if is_heartbeat { "heartbeat_ok" } else { "normal" }.to_string(),
                    ),
                },
            );

            if is_heartbeat {
                Ok(None)
            } else {
                Ok(Some(text))
            }
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
