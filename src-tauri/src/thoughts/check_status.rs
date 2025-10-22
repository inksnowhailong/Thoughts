use crate::globalConfig::config_struct::DataRestrict;
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
pub fn check_status(){

}
