use crate::models::AppConfig;
use std::fs;
use std::path::PathBuf;

/// 获取配置文件路径
fn config_path(app_data_dir: &PathBuf) -> PathBuf {
    app_data_dir.join("config.json")
}

/// 读取配置，不存在则返回默认值并写入
pub fn load_config(app_data_dir: &PathBuf) -> AppConfig {
    let path = config_path(app_data_dir);
    match fs::read_to_string(&path) {
        Ok(content) => serde_json::from_str(&content).unwrap_or_else(|_| {
            let config = AppConfig::default();
            save_config(app_data_dir, &config);
            config
        }),
        Err(_) => {
            let config = AppConfig::default();
            save_config(app_data_dir, &config);
            config
        }
    }
}

/// 保存配置到本地文件
pub fn save_config(app_data_dir: &PathBuf, config: &AppConfig) {
    let path = config_path(app_data_dir);
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(json) = serde_json::to_string_pretty(config) {
        let _ = fs::write(&path, json);
    }
}
