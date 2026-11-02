"""时间解析工具 —— 查询时间区间的唯一实现

素材筛选、用量统计都要把用户给的日期字符串解析成 datetime 做区间比较，
语义必须一致（纯日期含当天全天），所以抽到这一处，别复制第二份。"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from app.config import settings


_EXTENDED_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}(?:$|[T ])")


class TimeZoneConfigError(ValueError):
    pass


def utcnow() -> datetime:
    """naive UTC 当前时刻（替代已弃用的 datetime.utcnow()）。

    全库统一用这个：存储的 created_at/started_at/finished_at 都是 naive UTC，
    与 parse_day 的 naive 日期比较一致。直接改用 aware 会让比较抛 TypeError，
    所以这里取 aware 后剥掉时区信息，保持既有语义。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_day(s: str, end: bool = False) -> datetime:
    """把 '2026-10-29' / '2026-10-29T14:00' 解析成 datetime。
    用户输入按 APP_TIMEZONE 解释，最后转成数据库使用的 naive UTC。
    纯日期的 end=True 解析成本地当天 23:59:59（「到今天」包含全天）。"""
    if not _EXTENDED_ISO_PREFIX.match(s):
        raise ValueError("date must use extended ISO format")
    dt = datetime.fromisoformat(s)
    if end and len(s) == 10:  # 只有日期部分
        dt = dt + timedelta(days=1) - timedelta(seconds=1)
    if dt.tzinfo is None:
        try:
            local_zone = ZoneInfo(settings.APP_TIMEZONE)
        except ZoneInfoNotFoundError as exc:
            # Windows 的精简 Python 环境可能没有系统 IANA tzdata。默认时区给
            # 固定 UTC+8 回退；正式依赖同时声明 tzdata 以覆盖 DST 时区。
            if settings.APP_TIMEZONE == "Asia/Shanghai":
                local_zone = timezone(timedelta(hours=8))
            elif settings.APP_TIMEZONE == "UTC":
                local_zone = timezone.utc
            else:
                raise TimeZoneConfigError(f"未知时区：{settings.APP_TIMEZONE}") from exc
        dt = dt.replace(tzinfo=local_zone)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def parse_day_or_400(s: str, end: bool = False) -> datetime:
    """parse_day 的 API 入参包装：非法日期返回 400 而不是让 ValueError 冒泡成 500。"""
    try:
        return parse_day(s, end=end)
    except TimeZoneConfigError as exc:
        raise HTTPException(status_code=500, detail=f"服务端时区配置无效：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"日期格式不合法：{s}（应为 YYYY-MM-DD 或 ISO 8601 日期时间）",
        ) from exc
