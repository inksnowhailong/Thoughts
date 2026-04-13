// 数据的限制
#[derive(Debug, Default, Clone, Copy)]
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

impl DataRestrict {
    pub fn new(max_get_count: u32, max_get_frequency: u32, frequency_level: u8) -> Self {
        Self {
            get_count: 0,
            max_get_count,
            get_frequency: 0,
            max_get_frequency,
            frequency_level,
        }
    }
    // 登记一次执行，增加获取次数和获取频率
    pub fn register_call(&mut self) {
        self.get_count = self.get_count.saturating_add(1);
        self.get_frequency = self.get_frequency.saturating_add(1);
    }
}
