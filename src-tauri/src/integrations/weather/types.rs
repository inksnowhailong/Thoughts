use serde::Deserialize;
use thiserror::Error;
use time::error::Parse;
use time::macros::format_description;
use time::{Date, OffsetDateTime, PrimitiveDateTime, UtcOffset};

/// Open-Meteo 返回体中天气状况代码字段统一使用的数值类型。
pub type WeatherCode = u16;

/// 对外暴露的天气汇总信息。
///
/// * `current` 表示此刻天气。
/// * `today` 表示今天的概览。
/// * `tomorrow` 表示明天的预期概览。
#[derive(Debug, Clone)]
pub struct WeatherSnapshot {
    pub current: CurrentWeather,
    pub today: DailyWeather,
    pub tomorrow: DailyWeather,
}

/// 当前天气信息模型。
#[derive(Debug, Clone)]
pub struct CurrentWeather {
    pub observed_at: OffsetDateTime,
    pub temperature_celsius: f64,
    pub weather_code: WeatherCode,
}

/// 日间天气概览模型。
#[derive(Debug, Clone)]
pub struct DailyWeather {
    pub date: Date,
    pub temperature_max_celsius: f64,
    pub temperature_min_celsius: f64,
    pub weather_code: WeatherCode,
    pub sunrise: OffsetDateTime,
    pub sunset: OffsetDateTime,
}

/// 天气功能模块统一错误类型。
#[derive(Debug, Error)]
pub enum WeatherError {
    #[error("调用 Open-Meteo 接口失败: {0}")]
    Http(#[from] reqwest::Error),
    #[error("解析时间字段失败: {0}")]
    TimeParse(#[from] Parse),
    #[error("UTC 偏移量无效: {0}")]
    InvalidUtcOffset(i32),
    #[error("返回体缺失必须字段: {0}")]
    MissingData(&'static str),
    #[error("无法找到目标日期: {0}")]
    MissingDate(String),
}

/// Open-Meteo `current` 字段的原始结构。
#[derive(Debug, Deserialize)]
pub struct OpenMeteoCurrent {
    pub time: String,
    #[serde(rename = "temperature_2m")]
    pub temperature_2m: f64,
    #[serde(rename = "weather_code")]
    pub weather_code: WeatherCode,
}

/// Open-Meteo `daily` 字段的原始结构。
#[derive(Debug, Deserialize)]
pub struct OpenMeteoDaily {
    pub time: Vec<String>,
    #[serde(rename = "temperature_2m_max")]
    pub temperature_2m_max: Vec<f64>,
    #[serde(rename = "temperature_2m_min")]
    pub temperature_2m_min: Vec<f64>,
    #[serde(rename = "weathercode")]
    pub weathercode: Vec<WeatherCode>,
    pub sunrise: Vec<String>,
    pub sunset: Vec<String>,
}

/// Open-Meteo 顶层响应结构。
#[derive(Debug, Deserialize)]
pub struct OpenMeteoResponse {
    pub latitude: f64,
    pub longitude: f64,
    pub timezone: String,
    #[serde(rename = "utc_offset_seconds")]
    pub utc_offset_seconds: i32,
    pub current: OpenMeteoCurrent,
    pub daily: OpenMeteoDaily,
}

impl WeatherSnapshot {
    /// 由 Open-Meteo 响应体转换为领域模型。
    pub fn try_from_open_meteo(response: OpenMeteoResponse) -> Result<Self, WeatherError> {
        let offset = utc_offset(response.utc_offset_seconds)?;

        // 解析当前天气时间戳为 OffsetDateTime，便于后续计算日期。
        let observed_at = parse_datetime(&response.current.time, offset)?;
        let current = CurrentWeather {
            observed_at,
            temperature_celsius: response.current.temperature_2m,
            weather_code: response.current.weather_code,
        };

        // 将日间数组拆解为 DailyWeather 列表，方便查找今天和明天。
        let daily_entries = response
            .daily
            .into_daily_weather(offset)?
            .ok_or(WeatherError::MissingData("daily entries"))?;

        let today = daily_entries
            .iter()
            .find(|entry| entry.date == observed_at.date())
            .ok_or_else(|| WeatherError::MissingDate(format!("{}", observed_at.date())))?
            .clone();

        let tomorrow_date = observed_at.date().saturating_add(time::Duration::days(1));
        let tomorrow = daily_entries
            .iter()
            .find(|entry| entry.date == tomorrow_date)
            .ok_or_else(|| WeatherError::MissingDate(format!("{}", tomorrow_date)))?
            .clone();

        Ok(Self {
            current,
            today,
            tomorrow,
        })
    }
}

impl OpenMeteoDaily {
    /// 将分散的日间数组合并成结构化数据。
    fn into_daily_weather(
        self,
        offset: UtcOffset,
    ) -> Result<Option<Vec<DailyWeather>>, WeatherError> {
        if self.time.is_empty() {
            return Ok(None);
        }

        // 多个数组必须保持长度一致，否则说明响应体不完整。
        let len = self.time.len();
        if ![self.temperature_2m_max.len(), self.temperature_2m_min.len(), self.weathercode.len(), self.sunrise.len(), self.sunset.len()]
            .iter()
            .all(|&length| length == len)
        {
            return Err(WeatherError::MissingData("daily fields length mismatch"));
        }

        let mut daily = Vec::with_capacity(len);
        for idx in 0..len {
            let date = parse_date(&self.time[idx])?;
            let sunrise = parse_datetime(&self.sunrise[idx], offset)?;
            let sunset = parse_datetime(&self.sunset[idx], offset)?;

            daily.push(DailyWeather {
                date,
                temperature_max_celsius: self.temperature_2m_max[idx],
                temperature_min_celsius: self.temperature_2m_min[idx],
                weather_code: self.weathercode[idx],
                sunrise,
                sunset,
            });
        }

        Ok(Some(daily))
    }
}

/// Open-Meteo 日期时间统一转换帮助函数。
fn parse_datetime(raw: &str, offset: UtcOffset) -> Result<OffsetDateTime, WeatherError> {
    // Open-Meteo 此处返回形如 "2024-04-12T15:00" 的字符串，不含显式时区。
    let format = format_description!("[year]-[month]-[day]T[hour]:[minute]");
    let primitive = PrimitiveDateTime::parse(raw, &format)?;
    Ok(primitive.assume_offset(offset))
}

/// Open-Meteo 日期字符串 (YYYY-MM-DD) 转换帮助函数。
fn parse_date(raw: &str) -> Result<Date, WeatherError> {
    let format = format_description!("[year]-[month]-[day]");
    Ok(Date::parse(raw, &format)?)
}

/// 将偏移秒数转换为 `UtcOffset`，便于统一处理时间。
fn utc_offset(seconds: i32) -> Result<UtcOffset, WeatherError> {
    UtcOffset::from_whole_seconds(seconds).ok_or(WeatherError::InvalidUtcOffset(seconds))
}
