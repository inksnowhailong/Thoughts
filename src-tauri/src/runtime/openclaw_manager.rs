use tokio::process::Command;

/// 确保 Gateway 正在运行，若未运行则启动并等待就绪
pub async fn ensure_gateway_running(binary_path: &str) -> Result<(), String> {
    // 先检查是否已经在运行
    if health_check(binary_path).await {
        return Ok(());
    }

    // 不在运行，尝试启动
    Command::new(binary_path)
        .args(["gateway"])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map_err(|e| format!("启动 Gateway 失败: {}", e))?;

    // 轮询健康检查，最多等待 15 秒
    for _ in 0..15 {
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
        if health_check(binary_path).await {
            return Ok(());
        }
    }

    Err("Gateway 启动超时，健康检查未通过".to_string())
}

/// 停止 Gateway
pub async fn stop_gateway(binary_path: &str) -> Result<(), String> {
    let output = Command::new(binary_path)
        .args(["gateway", "stop"])
        .output()
        .await
        .map_err(|e| format!("停止 Gateway 失败: {}", e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!("停止 Gateway 失败: {}", stderr));
    }

    Ok(())
}

/// 健康检查：运行 `openclaw health`，返回是否健康
pub async fn health_check(binary_path: &str) -> bool {
    match Command::new(binary_path)
        .args(["health"])
        .output()
        .await
    {
        Ok(output) => output.status.success(),
        Err(_) => false,
    }
}
