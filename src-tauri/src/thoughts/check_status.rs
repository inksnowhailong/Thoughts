use crate::global_config::config_struct::DataRestrict;
use crate::integrations::weather::fetch_weather_overview;
// 气温情况
struct Temperature {
    // 今日最低气温
    pub day_min: f32,
    // 今日最高气温
    pub day_max: f32,
    // 当前温度
    pub now: f32,
    pub data_restrict:DataRestrict
}
// 世界感知状态数据
pub struct WorldStatus {
    pub time: String,
    pub temperature: Temperature,
}


// 检查状态
pub async fn check_status(){
     match fetch_weather_overview(39.9042, 116.4074, Some("Asia/Shanghai"), Some("zh")).await {
        Ok(weather) => {
            // 输出当前天气
            println!("当前天气:");
            println!("  时间: {}", weather.current.observed_at);
            println!("  温度: {}°C", weather.current.temperature_celsius);
            println!("  天气代码: {}", weather.current.weather_code);

            // 输出今日天气
            println!("\n今日天气:");
            println!("  日期: {}", weather.today.date);
            println!("  最高温度: {}°C", weather.today.temperature_max_celsius);
            println!("  最低温度: {}°C", weather.today.temperature_min_celsius);
            println!("  天气代码: {}", weather.today.weather_code);
            println!("  日出: {}", weather.today.sunrise);
            println!("  日落: {}", weather.today.sunset);

            // 输出明日天气
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
