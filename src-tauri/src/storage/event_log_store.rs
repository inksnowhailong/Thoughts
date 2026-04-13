use crate::models::EventLog;
use chrono::Local;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;

/// 获取今天的事件日志文件路径
fn today_log_path(app_data_dir: &PathBuf) -> PathBuf {
    let date = Local::now().format("%Y-%m-%d");
    app_data_dir.join("logs").join("events").join(format!("{}.jsonl", date))
}

/// 追加一条事件日志
pub fn append_event(app_data_dir: &PathBuf, event: &EventLog) {
    let path = today_log_path(app_data_dir);
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(json) = serde_json::to_string(event) {
        if let Ok(mut file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
        {
            let _ = writeln!(file, "{}", json);
        }
    }
}

/// 生成事件 ID
pub fn generate_event_id(trigger: &str) -> String {
    let now = Local::now();
    format!("evt_{}_{}", now.format("%Y%m%d_%H%M%S"), trigger)
}
