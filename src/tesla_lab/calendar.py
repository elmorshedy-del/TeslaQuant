from functools import lru_cache
import exchange_calendars as xc
import pandas as pd


@lru_cache(maxsize=1)
def exchange():
    return xc.get_calendar("XNYS", start="1990-01-01", end="2035-12-31")


def sessions(start, end) -> pd.DatetimeIndex:
    return exchange().sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))


def session_date(value) -> str:
    d = pd.Timestamp(value)
    if d.tzinfo is not None:
        d = d.tz_convert("America/New_York").tz_localize(None)
    d = d.normalize()
    if not exchange().is_session(d):
        raise ValueError(f"Not an exchange session: {d.date()}")
    return d.date().isoformat()


def cutoff(value) -> pd.Timestamp:
    return pd.Timestamp(session_date(value) + " 09:00", tz="America/New_York").tz_convert("UTC")
