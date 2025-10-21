
use crate::config::LOOP_TIME;
use std::thread;
use std::time::Duration;

// 思绪核心
pub fn thoughts_core(){
    let (tx,rx) = channel::<()>();
    let handle = thread::spawn(move || {
        loop {
            match rx.recv_timout(Duration::from_secs(LOOP_TIME)) {
                Ok(_) => {
                    // 收到停止信号，退出循环
                    break;
                }
                Err(RecvTimeoutError::Timeout) => {
                    // 执行思绪核心的主要逻辑
                    // 检查状态
                    if let Err(e) = check_status(){
                        eprintln!("Error checking status: {}", e);
                    }
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

fn check_status(){

}
