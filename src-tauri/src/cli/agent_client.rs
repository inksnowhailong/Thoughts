use super::types::CliResponse;
use std::time::Duration;
use tokio::process::Command;

/// 调用 OpenClaw CLI 发送消息并获取 JSON 响应
pub async fn send_to_openclaw(
    binary_path: &str,
    message: &str,
    session_key: &str,
    timeout_secs: u64,
) -> Result<CliResponse, String> {
    let output = tokio::time::timeout(
        Duration::from_secs(timeout_secs + 5),
        Command::new(binary_path)
            .args([
                "agent",
                "-m",
                message,
                "--json",
                "--session-id",
                session_key,
                "--timeout",
                &timeout_secs.to_string(),
            ])
            .output(),
    )
    .await
    .map_err(|_| "请求超时".to_string())?
    .map_err(|e| format!("执行 openclaw 命令失败: {}", e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!("openclaw 命令执行失败: {}", stderr));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str::<CliResponse>(&stdout)
        .map_err(|e| format!("解析 JSON 失败: {} | 原始输出: {}", e, stdout))
}

/// 从 CliResponse 中提取文本内容
pub fn extract_text(response: &CliResponse) -> Result<String, String> {
    if let Some(ref error) = response.result.meta.error {
        return Err(format!("[{}] {}", error.kind, error.message));
    }
    if response.status != "ok" {
        return Err(format!("非正常状态: {}", response.status));
    }
    // 拼接所有 payload 的文本内容
    let texts: Vec<String> = response
        .result
        .payloads
        .iter()
        .filter_map(|p| p.text.clone())
        .collect();
    if texts.is_empty() {
        return Err("返回内容为空".to_string());
    }
    Ok(texts.join("\n\n"))
}
