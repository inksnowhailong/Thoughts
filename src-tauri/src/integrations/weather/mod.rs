//! 天气集成模块。
//!
//! 该模块当前使用 Open-Meteo 作为外部天气服务提供商，暴露出一个便捷的主函数
//! [`fetch_weather_overview`]，用于一次性获取当前、今天及明日的天气核心信息。

pub mod open_meteo;
pub mod types;

pub use types::{WeatherError, WeatherSnapshot};

use open_meteo::OpenMeteoClient;

/// 主函数：基于经纬度从 Open-Meteo 拉取天气概览。
///
/// # 参数
/// * `latitude` / `longitude` - 使用 WGS84 坐标系的十进制度数。
/// * `timezone` - 传入如 `"Asia/Shanghai"` 的时区名称；若为 `None` 则自动推断。
/// * `language` - 可选的语言代码，遵循 ISO 639-1，例如 `"zh"`。
///
/// # 返回
/// 若请求成功，将得到 [`WeatherSnapshot`]，包含当前、今日与明日的天气信息。
pub async fn fetch_weather_overview(
    latitude: f64,
    longitude: f64,
    timezone: Option<&str>,
    language: Option<&str>,
) -> Result<WeatherSnapshot, WeatherError> {
    // 组装 Open-Meteo 客户端，需要时区或语言时使用构建器链式配置。
    let mut client = OpenMeteoClient::new(latitude, longitude)?;
    if let Some(tz) = timezone {
        client = client.with_timezone(tz);
    }
    if let Some(lang) = language {
        client = client.with_language(lang);
    }

    client.weather_snapshot().await
}
