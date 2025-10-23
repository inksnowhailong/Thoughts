use reqwest::{Client, Url};
use std::time::Duration;

use super::types::{OpenMeteoResponse, WeatherError, WeatherSnapshot};

/// Open-Meteo HTTP 客户端，负责与真实 API 通信。
#[derive(Debug, Clone)]
pub struct OpenMeteoClient {
    http: Client,
    base_url: Url,
    latitude: f64,
    longitude: f64,
    timezone: String,
    language: Option<String>,
}

impl OpenMeteoClient {
    /// 常量 base url，Open-Meteo 提供的 v1 forecast 终端。
    const BASE_URL: &'static str = "https://api.open-meteo.com/v1/forecast";

    /// 构建默认客户端。
    ///
    /// - `latitude` 与 `longitude` 必须为十进制度数。
    /// - 默认使用 `auto` 时区，由 Open-Meteo 根据坐标推断。
    pub fn new(latitude: f64, longitude: f64) -> Result<Self, WeatherError> {
        let http = Client::builder()
            .timeout(Duration::from_secs(10))
            .user_agent("thoughts-app/1.0 (+https://github.com/)")
            .build()?;

        let base_url = Url::parse(Self::BASE_URL).expect("Open-Meteo base url is valid");

        Ok(Self {
            http,
            base_url,
            latitude,
            longitude,
            timezone: "auto".to_string(),
            language: None,
        })
    }

    /// 指定语言代码，便于获取本地化的天气描述。
    /// 可选项，未设置则由 Open-Meteo 使用默认语言（英语）。
    pub fn with_language(mut self, language: impl Into<String>) -> Self {
        self.language = Some(language.into());
        self
    }

    /// 指定时区名称，例如 `"Asia/Shanghai"`。
    /// 不调用时仍使用 `auto` 自动推断。
    pub fn with_timezone(mut self, timezone: impl Into<String>) -> Self {
        self.timezone = timezone.into();
        self
    }

    /// 调用 Open-Meteo 接口并转换为领域模型。
    pub async fn weather_snapshot(&self) -> Result<WeatherSnapshot, WeatherError> {
        let mut url = self.base_url.clone();
        {
            // 组装查询参数，确保取到当前 + 两天的日间信息和必要字段。
            let mut pairs = url.query_pairs_mut();
            pairs.append_pair("latitude", &self.latitude.to_string());
            pairs.append_pair("longitude", &self.longitude.to_string());
            pairs.append_pair("timezone", &self.timezone);
            pairs.append_pair("forecast_days", "3");
            pairs.append_pair("current", "temperature_2m,weather_code");
            pairs.append_pair(
                "daily",
                "temperature_2m_max,temperature_2m_min,weathercode,sunrise,sunset",
            );
            if let Some(lang) = &self.language {
                pairs.append_pair("language", lang);
            }
        }

        let response = self
            .http
            .get(url)
            .send()
            .await?
            .error_for_status()?; // HTTP 非 2xx 自动转为错误，避免继续解析。

        let payload = response.json::<OpenMeteoResponse>().await?;
        WeatherSnapshot::try_from_open_meteo(payload)
    }
}
