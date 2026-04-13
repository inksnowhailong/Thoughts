use crate::global_config::config_struct::DataRestrict;
use crate::integrations::weather::{fetch_weather_overview, WeatherSnapshot};

#[derive(Debug, Clone, Copy)]
struct Temperature {
    day_min: f32,
    day_max: f32,
    now: f32,
    data_restrict: DataRestrict,
}

impl Temperature {
    fn from_snapshot(snapshot: &WeatherSnapshot, data_restrict: DataRestrict) -> Self {
        Self {
            day_min: snapshot.today.temperature_min_celsius as f32,
            day_max: snapshot.today.temperature_max_celsius as f32,
            now: snapshot.current.temperature_celsius as f32,
            data_restrict,
        }
    }
}

#[derive(Debug, Clone)]
struct WorldStatus {
    observed_at: String,
    temperature: Temperature,
}

impl WorldStatus {
    fn from_snapshot(snapshot: &WeatherSnapshot, data_restrict: DataRestrict) -> Self {
        Self {
            observed_at: snapshot.current.observed_at.to_string(),
            temperature: Temperature::from_snapshot(snapshot, data_restrict),
        }
    }
}
// 检查状态
pub async fn check_status() {
    let mut data_restrict = DataRestrict::new(60, 12, 2);
    data_restrict.register_call();

    match fetch_weather_overview(39.9042, 116.4074, Some("Asia/Shanghai"), Some("zh")).await {
        Ok(weather) => {
            let status = WorldStatus::from_snapshot(&weather, data_restrict);

            println!("当前天气:");
            println!("  时间: {}", status.observed_at);
            println!("  温度: {}°C", status.temperature.now);
            println!(
                "  今日温度区间: {}°C - {}°C",
                status.temperature.day_min, status.temperature.day_max
            );
            println!(
                "  请求配额: {}/{} 次 (频率等级 {}, 当前频率: {}/{})",
                status.temperature.data_restrict.get_count,
                status.temperature.data_restrict.max_get_count,
                status.temperature.data_restrict.frequency_level,
                status.temperature.data_restrict.get_frequency,
                status.temperature.data_restrict.max_get_frequency
            );
            println!("  天气代码: {}", weather.current.weather_code);

            println!("\n今日天气:");
            println!("  日期: {}", weather.today.date);
            println!("  最高温度: {}°C", weather.today.temperature_max_celsius);
            println!("  最低温度: {}°C", weather.today.temperature_min_celsius);
            println!("  天气代码: {}", weather.today.weather_code);
            println!("  日出: {}", weather.today.sunrise);
            println!("  日落: {}", weather.today.sunset);

            println!("\n明日天气:");
            println!("  日期: {}", weather.tomorrow.date);
            println!("  最高温度: {}°C", weather.tomorrow.temperature_max_celsius);
            println!("  最低温度: {}°C", weather.tomorrow.temperature_min_celsius);
            println!("  天气代码: {}", weather.tomorrow.weather_code);
        }
        Err(e) => {
            eprintln!("获取天气信息失败: {}", e);
        }
    }
}
