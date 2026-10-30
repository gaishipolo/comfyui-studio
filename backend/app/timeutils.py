"""时间解析工具 —— 查询时间区间的唯一实现

素材筛选、用量统计都要把用户给的日期字符串解析成 datetime 做区间比较，
语义必须一致（纯日期含当天全天），所以抽到这一处，别复制第二份。"""
from datetime import datetime, timedelta


def parse_day(s: str, end: bool = False) -> datetime:
    """把 '2026-10-29' / '2026-10-29T14:00' 解析成 datetime。
    纯日期的 end=True 解析成当天 23:59:59（「到今天」要包含今天全天）。"""
    dt = datetime.fromisoformat(s)
    if end and len(s) == 10:  # 只有日期部分
        dt = dt + timedelta(days=1) - timedelta(seconds=1)
    return dt
