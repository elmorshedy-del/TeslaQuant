"""Append-only price-only review versions; unknown outcomes stay unknown."""
import json
from copy import deepcopy
import time
import numpy as np
import pandas as pd
from tesla_lab.calendar import session_date
from tesla_lab.ledger import canonical, identity


def validate_review(body):
    if not isinstance(body, dict):
        raise ValueError("Review document must be a JSON object")
    body = deepcopy(body)
    if not body.get("rubric") or not isinstance(body.get("reviewed_intervals"), list) or not isinstance(body.get("episodes"), list):
        raise ValueError("Rubric, reviewed intervals and episodes required")
    lag = body.get("confirmation_sessions")
    if type(lag) is not int or not 0 <= lag <= 60:
        raise ValueError("Explicit integer confirmation_sessions from 0 through 60 required")
    intervals = []
    for interval in body["reviewed_intervals"]:
        if not isinstance(interval, dict) or not {"start", "end"} <= interval.keys():
            raise ValueError("Review intervals require start and end")
        start, end = session_date(interval["start"]), session_date(interval["end"])
        interval.update(start=start, end=end)
        if start > end:
            raise ValueError("Review interval reversed")
        intervals.append((start, end))
    previous_end = None
    for episode in body["episodes"]:
        if not isinstance(episode, dict) or not {"onset", "confirmation", "end"} <= episode.keys():
            raise ValueError("Episodes require onset, confirmation and end")
        for key in ("onset", "confirmation", "end"):
            episode[key] = session_date(episode[key])
    body["episodes"].sort(key=lambda e: e["onset"])
    for episode in body["episodes"]:
        onset = session_date(episode["onset"])
        confirmation = session_date(episode["confirmation"])
        end = session_date(episode["end"])
        if not onset <= confirmation <= end:
            raise ValueError("Episode dates must satisfy onset <= confirmation <= end")
        if previous_end and onset <= previous_end:
            raise ValueError("Episodes overlap; resolve grouping in the annotation rubric")
        if not any(start <= onset and end <= finish for start, finish in intervals):
            raise ValueError("Episode must be inside a completely reviewed interval")
        previous_end = end
    return body


def save_review(ledger, body):
    body = validate_review(body)
    rid = identity(body)
    with ledger.connect() as c:
        c.execute("INSERT OR IGNORE INTO reviews VALUES(?,?,?)", (rid, canonical(body), time.time()))
    return rid


def get_review(ledger, rid):
    with ledger.connect() as c:
        row = c.execute("SELECT body FROM reviews WHERE id=?", (rid,)).fetchone()
    if not row:
        raise ValueError("Annotation version not found")
    return json.loads(row[0])


def list_reviews(ledger):
    with ledger.connect() as c:
        return [{"id": r[0], "body": json.loads(r[1])} for r in c.execute("SELECT id,body FROM reviews ORDER BY created_at DESC")]


def make_labels(index, review, horizon=10):
    review = validate_review(review)
    if horizon not in {5, 10}:
        raise ValueError("Supported warning horizons are 5 and 10 sessions")
    index = pd.DatetimeIndex(index)
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError("Label calendar must be ordered and unique")
    out = pd.DataFrame({"target": np.nan, "label_end": pd.NaT, "episode": None}, index=index)
    intervals = [(pd.Timestamp(i["start"]), pd.Timestamp(i["end"])) for i in review["reviewed_intervals"]]
    episodes = [{k: pd.Timestamp(e[k]) for k in ("onset", "confirmation", "end")} for e in review["episodes"]]
    for n, date in enumerate(index):
        if n + horizon - 1 >= len(index):
            continue
        end = index[n + horizon - 1]  # Includes the forecast's session: pre-open warning.
        if any(e["onset"] < date <= e["end"] for e in episodes):
            continue  # Continuation is not an eligible onset context.
        candidates = [e for e in episodes if date <= e["onset"] <= end]
        if candidates:
            end = max(end, max(e["confirmation"] for e in candidates))
        # For negative labels, classification may itself need forward confirmation.
        confirmation_lag = review["confirmation_sessions"]
        end_position = index.searchsorted(end) + confirmation_lag
        if end_position >= len(index):
            continue
        end = index[end_position]
        if not any(start <= date and end <= finish for start, finish in intervals):
            continue
        out.loc[date, "target"] = float(bool(candidates))
        out.loc[date, "label_end"] = end
        if candidates:
            out.loc[date, "episode"] = candidates[0]["onset"].date().isoformat()
    return out
