import calendar
import datetime


def month_end(year: int, month: int) -> datetime.date:
    """Last calendar day of the given month."""
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def year_end(year: int) -> datetime.date:
    """December 31st of the given year."""
    return datetime.date(year, 12, 31)
