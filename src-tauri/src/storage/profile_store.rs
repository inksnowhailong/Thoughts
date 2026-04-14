use serde_json::Value;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tokio::sync::Mutex;

/// 用户画像存储，负责本地 JSON 文件的读写与 merge
/// 使用 Arc<Mutex<>> 包装以支持并发安全的 Tauri managed state
pub struct ProfileStore {
    path: PathBuf,
    /// 文件级互斥锁，防止并发读写
    lock: Mutex<()>,
}

impl ProfileStore {
    pub fn new(app_data_dir: &Path) -> Arc<Self> {
        Arc::new(Self {
            path: app_data_dir.join("user-profile.json"),
            lock: Mutex::new(()),
        })
    }

    /// 画像文件是否存在
    pub fn exists(&self) -> bool {
        self.path.exists()
    }

    /// 读取画像 JSON，文件不存在或解析失败返回 None
    pub async fn load(&self) -> Option<Value> {
        let _guard = self.lock.lock().await;
        let content = std::fs::read_to_string(&self.path).ok()?;
        serde_json::from_str(&content).ok()
    }

    /// 写入画像 JSON（全量覆盖）
    pub async fn save(&self, value: &Value) -> Result<(), String> {
        let _guard = self.lock.lock().await;
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("创建目录失败: {}", e))?;
        }
        let json = serde_json::to_string_pretty(value)
            .map_err(|e| format!("序列化 JSON 失败: {}", e))?;
        std::fs::write(&self.path, json)
            .map_err(|e| format!("写入画像文件失败: {}", e))
    }

    /// 读取现有画像，shallow merge 新字段后写回
    /// 顶层 key 遍历，新值覆盖旧值（数组为 replace 语义）
    pub async fn merge(&self, partial: &Value) -> Result<Value, String> {
        let _guard = self.lock.lock().await;

        // 读取现有画像，不存在则以空对象开始
        let mut existing = match std::fs::read_to_string(&self.path) {
            Ok(content) => serde_json::from_str::<Value>(&content)
                .unwrap_or_else(|_| Value::Object(serde_json::Map::new())),
            Err(_) => Value::Object(serde_json::Map::new()),
        };

        // shallow merge：仅合并顶层字段
        if let (Some(base), Some(updates)) = (existing.as_object_mut(), partial.as_object()) {
            for (key, value) in updates {
                base.insert(key.clone(), value.clone());
            }
        } else {
            return Err("画像数据格式错误：需要 JSON Object".to_string());
        }

        // 写回
        if let Some(parent) = self.path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let json = serde_json::to_string_pretty(&existing)
            .map_err(|e| format!("序列化 JSON 失败: {}", e))?;
        std::fs::write(&self.path, &json)
            .map_err(|e| format!("写入画像文件失败: {}", e))?;

        Ok(existing)
    }
}
