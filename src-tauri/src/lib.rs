use crate::thoughts::core::thoughts_core;


mod thoughts;
mod global_config;
mod integrations;
// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let (stop_tx, worker_handle) = thoughts_core();

    // 程序要退出时：
    let _ = stop_tx.send(());       // 发一个停止信号；或者直接 drop 掉所有 Sender
    let _ = worker_handle.join();   // 等后台线程收尾

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![greet])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
