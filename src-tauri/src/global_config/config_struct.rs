// 数据的限制
pub struct DataRestrict {
    // 获取次数
    pub get_count: u32,
    // 最大获取次数
    pub max_get_count: u32,
    //获取频率
    pub get_frequency: u32,
    //最大获取频率
    pub max_get_frequency: u32,
    // 频率级别 (1-5) 秒、分、时、天、月
    pub frequency_level: u8,
}