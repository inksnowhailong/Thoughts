mod app;
mod cli;
mod commands;
mod models;
mod runtime;
mod storage;

use app::state::create_shared_state;
use models::{AppPhase, GatewayStatus};
use runtime::{openclaw_manager, scheduler};
use storage::{config_store, profile_store::ProfileStore};
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let app_handle = app.handle().clone();
            let app_data_dir = app_handle
                .path()
                .app_data_dir()
                .expect("无法获取应用数据目录");

            // 加载配置
            let config = config_store::load_config(&app_data_dir);
            let binary_path = config.openclaw_binary_path.clone();

            // 创建 ProfileStore 并注入 Tauri managed state
            let profile_store = ProfileStore::new(&app_data_dir);
            let needs_onboarding = !profile_store.exists();
            let app_phase = if needs_onboarding {
                AppPhase::Onboarding
            } else {
                AppPhase::Active
            };
            app.manage(profile_store);

            // 创建共享状态并注册到 Tauri
            let state = create_shared_state(config, app_phase.clone());

            // 创建 onboarding 完成信号通道
            let (onboarding_tx, onboarding_rx) = tokio::sync::oneshot::channel::<()>();
            if !needs_onboarding {
                // 不需要 onboarding，立即发送信号让调度器启动
                let _ = onboarding_tx.send(());
            } else {
                // 需要 onboarding，将信号发送端存入 AppState
                let state_clone = state.clone();
                tauri::async_runtime::spawn(async move {
                    let mut s = state_clone.write().await;
                    s.onboarding_signal = Some(onboarding_tx);
                });
            }

            app.manage(state.clone());

            // 创建调度器停止信号
            let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);

            // 将 stop_tx 存储起来，应用退出时发送停止信号
            let stop_tx_for_exit = std::sync::Mutex::new(Some(stop_tx));
            let binary_for_exit = binary_path.clone();

            // 后台启动 Gateway 和调度器
            let state_for_boot = state.clone();
            tauri::async_runtime::spawn(async move {
                // 1. 启动 Gateway
                {
                    let mut s = state_for_boot.write().await;
                    s.gateway_status = GatewayStatus::Starting;
                }

                match openclaw_manager::ensure_gateway_running(&binary_path).await {
                    Ok(()) => {
                        let mut s = state_for_boot.write().await;
                        s.gateway_status = GatewayStatus::Ready;
                        s.last_error = None;
                    }
                    Err(e) => {
                        let mut s = state_for_boot.write().await;
                        s.gateway_status = GatewayStatus::Error(e.clone());
                        s.last_error = Some(e);
                        return; // Gateway 启动失败，不启动调度器
                    }
                }

                // 2. 等待 onboarding 完成信号（非 onboarding 场景立即通过）
                if onboarding_rx.await.is_err() {
                    // 信号发送端被 drop（应用退出），不启动调度器
                    return;
                }

                // 3. 启动自动轮询调度器
                scheduler::run_scheduler(state_for_boot, app_handle, app_data_dir, stop_rx).await;
            });

            // 监听窗口关闭事件，清理资源
            let window = app.get_webview_window("main").expect("找不到主窗口");
            window.on_window_event(move |event| {
                if let tauri::WindowEvent::Destroyed = event {
                    // 发送停止信号给调度器
                    if let Ok(mut guard) = stop_tx_for_exit.lock() {
                        if let Some(tx) = guard.take() {
                            let _ = tx.send(true);
                        }
                    }
                    // 尝试停止 Gateway
                    let binary = binary_for_exit.clone();
                    tauri::async_runtime::spawn(async move {
                        let _ = openclaw_manager::stop_gateway(&binary).await;
                    });
                }
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::send_user_message,
            commands::get_status,
            commands::trigger_manual_poll,
            commands::restart_gateway,
            commands::send_onboarding_message,
            commands::get_user_profile,
            commands::skip_onboarding,
        ])
        .run(tauri::generate_context!())
        .expect("启动应用失败");
}
