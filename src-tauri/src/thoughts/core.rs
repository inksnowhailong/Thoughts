use super::check_status::check_status;
use crate::global_config::config::LOOP_TIME;
use std::future::Future;
use std::time::Duration; // 从同级模块引入函数
use std::pin::Pin;
use tauri::async_runtime::{self, JoinHandle };
use tokio::sync::oneshot;
use tokio::time::{interval, Interval};
use tokio::select;
type ThoughtTask = fn() -> Pin<Box<dyn Future<Output = ()> + Send>>;
// 思绪核心
pub fn thoughts_core() -> (oneshot::Sender<()>, JoinHandle<()>) {
    // 创建一个 oneshot 通道，用于发送停止信号
    let (tx, rx) = oneshot::channel::<()>();

    let tasks: Vec<ThoughtTask> = vec![|| Box::pin(check_status())];
    let mut ticker = interval(Duration::from_secs(LOOP_TIME));
    let handle = async_runtime::spawn(async move {
        run_loop(&mut ticker, rx, &tasks).await;
    });
    (tx, handle)
}

async fn run_loop(
    ticker: &mut Interval,
    mut rx: oneshot::Receiver<()>,
    tasks: &[ThoughtTask],
) {
    loop {
        select! {
            _ = &mut rx => break,
            _ = ticker.tick() => {
                for task in tasks {
                    task().await;
                }
            }
        }
    }
}
