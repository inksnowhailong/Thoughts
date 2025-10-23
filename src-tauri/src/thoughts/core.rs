
use crate::global_config::config::LOOP_TIME;
use std::sync::mpsc::{channel, Sender, RecvTimeoutError};
use std::thread::{self, JoinHandle};
use std::time::Duration;
use super::check_status::check_status; // 从同级模块引入函数

// 思绪核心
pub fn thoughts_core()-> (Sender<()>, JoinHandle<()>){

    let throughts = vec!(check_status);

    let (tx,rx) = channel::<()>();
    let handle = thread::spawn(move || {
        loop {
            match rx.recv_timeout(Duration::from_secs(LOOP_TIME)) {
                Ok(_) => {
                    // 收到停止信号，退出循环
                    break;
                }
                Err(RecvTimeoutError::Timeout) => {
                    // 执行思绪核心的主要逻辑
                    for throught in &throughts {
                        throught();
                    };
                }
                Err(_) => {
                    // 超时，继续执行任务
                    // 执行思绪核心的主要逻辑
                }
            }
        }
    });
    (tx, handle)
}
