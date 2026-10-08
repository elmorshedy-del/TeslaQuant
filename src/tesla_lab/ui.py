"""Phone-friendly controls; heavy work belongs to the persistent worker."""
from datetime import date
import json
import pandas as pd
import streamlit as st
from tesla_lab.config import data_root
from tesla_lab.data import Lake
from tesla_lab.episodes import get_review, list_reviews, save_review
from tesla_lab.ledger import Ledger
from tesla_lab.providers import reference_annotations


def dataset_label(d):
    return f"{d['kind']} · {d['metadata']['provider']} · {d['rows']:,} rows · {d['id'][:10]}"


def job_table(ledger):
    jobs = ledger.jobs()
    if not jobs:
        st.caption("No jobs yet. Jobs keep running after you close this page.")
        return
    rows = [{"Job": j["id"][:10], "Task": j["kind"], "Status": j["status"], "Attempts": j["attempts"], "Reason": j["error"] or ""} for j in jobs]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    retryable = [j for j in jobs if j["status"] in {"failed", "blocked"}]
    if retryable:
        selected = st.selectbox("Retry after resolving the reason", retryable, format_func=lambda j: f"{j['kind']} · {j['id'][:10]}")
        if st.button("Retry selected job"):
            ledger.retry(selected["id"])
            st.rerun()


def data_view(ledger, lake):
    st.header("Episodes / Data")
    st.write("Review price paths, preserve the original examples, and track data coverage.")
    datasets = ledger.datasets()
    if datasets:
        choice = st.selectbox("Dataset version", datasets, format_func=dataset_label)
        frame = lake.read(choice["id"])
        st.caption(f"Availability: {choice['metadata']['availability']} · immutable version {choice['id'][:16]}")
        if choice["kind"] == "equity_daily":
            stock = frame.loc[frame.symbol == "TSLA"].set_index(pd.to_datetime(frame.loc[frame.symbol == "TSLA", "session"]))
            if not stock.empty:
                st.line_chart(stock[["close"]], height=250)
                st.caption(f"Coverage {stock.session.min()} through {stock.session.max()}. Missing exchange sessions are retained by the evaluator.")
        if choice["kind"] == "gamma_daily":
            st.write(frame.quality.value_counts().rename("rows"))
        st.dataframe(frame.tail(20), hide_index=True, width="stretch")
        with st.expander("Provenance and measurement contract"):
            st.json(choice["metadata"])
    else:
        st.info("Import the existing audit to begin, or request daily history from the provider.")
    with st.expander("Import the previous audit", expanded=not bool(datasets)):
        upload = st.file_uploader("Tesla_Model_Audit archive", type=["zip"])
        if st.button("Queue audit import", disabled=upload is None):
            body = upload.getvalue()
            if len(body) > 100 * 1024**2:
                st.error("Upload limit is 100 MB.")
            else:
                rid = lake.raw(body, {"type": "uploaded_audit", "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat()})
                jid = ledger.enqueue("import_audit", {"path": f"raw/{rid}"})
                st.success(f"Import queued · {jid[:10]}")
    with st.expander("Request daily history"):
        st.caption("Provider keys are configured on the worker. This uses consolidated, split-adjusted daily bars.")
        with st.form("daily_request"):
            start = st.date_input("History begins", value=date(2016, 1, 4))
            end = st.date_input("History ends", value=date.today())
            symbols = st.multiselect("Symbols", ["TSLA", "SPY", "QQQ"], default=["TSLA", "SPY", "QQQ"])
            if st.form_submit_button("Queue daily download"):
                jid = ledger.enqueue("ingest_alpaca", {"start": start.isoformat(), "end": end.isoformat(), "symbols": symbols})
                st.success(f"Download queued · {jid[:10]}")
    with st.expander("Original reference weeks"):
        st.dataframe(pd.DataFrame(reference_annotations(ledger)), hide_index=True)
        st.caption("These are supplied week identifiers. Exact daily onsets remain unconfirmed; September 1, 2025 was a market holiday.")
    with st.expander("Save a complete price-only review"):
        st.write("Record the rubric, every episode, and only intervals whose full price paths have been reviewed. Saving a review creates a new immutable version.")
        st.caption("A JSON review contains rubric, confirmation_sessions, reviewed_intervals and episodes (onset, confirmation, end). See the example in docs/review-format.md.")
        text = st.text_area("Review document", height=180, placeholder='{"rubric":"price-only-v1","confirmation_sessions":5,"reviewed_intervals":[],"episodes":[],"note":""}')
        attest = st.checkbox("This review is complete within its declared intervals and was made from price paths independently of predictors")
        if st.button("Save review version", disabled=not attest or not text.strip()):
            try:
                rid = save_review(ledger, json.loads(text))
                st.success(f"Review saved · {rid[:12]}")
            except (ValueError, KeyError, TypeError) as e:
                st.error(str(e))


def experiment_view(ledger):
    st.header("Experiments")
    st.write("Run the fixed daily benchmark against reviewed onset outcomes. Every input version and attempted configuration is retained.")
    datasets = ledger.datasets("equity_daily")
    reviews = list_reviews(ledger)
    if not datasets or not reviews:
        st.info("A daily dataset and a complete price-only review are required before model evaluation.")
    else:
        with st.form("experiment"):
            dataset = st.selectbox("Daily dataset", datasets, format_func=dataset_label)
            review = st.selectbox("Annotation version", reviews, format_func=lambda r: f"{r['body']['rubric']} · {r['id'][:10]}")
            horizon = st.selectbox("Warning horizon (trading sessions)", [10, 5])
            budget = st.selectbox("Validation alert budget per year", [4, 8])
            st.caption("Three-year initial training, six-month outer tests, purged outcomes and a ten-session alert cooldown. Historical evaluations remain experimental.")
            if st.form_submit_button("Run saved benchmark"):
                config = {"protocol": "daily-baseline-v1", "dataset_id": dataset["id"], "review_id": review["id"], "horizon": horizon, "alert_budget": budget}
                jid = ledger.enqueue("experiment", config)
                st.success(f"Experiment queued · {jid[:10]}")
    st.subheader("Jobs")
    job_table(ledger)


def evidence_view(ledger):
    st.header("Evidence")
    reports = [j for j in ledger.jobs() if j["kind"] == "experiment" and j["status"] == "succeeded"]
    if not reports:
        st.info("No completed research report yet. Imported data and plausible theory are not a validated predictive edge.")
        return
    job = st.selectbox("Report", reports, format_func=lambda j: j["id"][:12])
    path = ledger.root / job["result"]["report_path"]
    report = json.loads(path.read_text())
    st.caption("Retrospective experimental evidence · no automatic promotion")
    metrics = report["metrics"]
    left, right = st.columns(2)
    left.metric("Onsets detected", metrics["onsets_detected"])
    right.metric("False alerts", metrics["false_alerts"])
    st.json(metrics)
    st.dataframe(pd.DataFrame(report["predictions"]), hide_index=True, width="stretch")
    with st.expander("Chronological folds and limitations"):
        st.dataframe(pd.DataFrame(report["folds"]), hide_index=True)
        st.write(report["limitations"])
    with st.expander("Calibration assessment"):
        st.dataframe(pd.DataFrame(report["calibration"]), hide_index=True)
        st.caption("Descriptive held-out probability bins. Adjacent dates are dependent; uncertainty intervals remain a later research gate.")
    st.download_button("Download full report", path.read_bytes(), file_name="tesla-experiment.json", mime="application/json")


def forecast_view(ledger):
    st.header("Current Forecast")
    st.info("Forecast blocked: no frozen, prospectively eligible model has been approved. The app will not display a historical backtest score as today's probability.")
    with ledger.connect() as c:
        rows = c.execute("SELECT body FROM forecasts ORDER BY created_at DESC LIMIT 30").fetchall()
    if rows:
        st.dataframe(pd.DataFrame([json.loads(r[0]) for r in rows]), hide_index=True)
    st.caption("Original forecasts have an append-only ledger. Model promotion and live input checks are a later research gate.")


def main():
    st.set_page_config(page_title="Tesla Research Lab", page_icon="📈", layout="centered")
    st.title("Tesla Research Lab")
    st.caption("Demand · absorption · feedback | Research infrastructure 0.1")
    ledger = Ledger(data_root())
    lake = Lake(ledger)
    view = st.sidebar.radio("Workspace", ["Episodes / Data", "Experiments", "Evidence", "Current Forecast"])
    st.sidebar.caption("Jobs persist when this browser closes.")
    if st.sidebar.button("Refresh"):
        st.rerun()
    if st.button("Check worker connection", key="inspect_job"):
        jid = ledger.enqueue("inspect", {"requested_at": pd.Timestamp.now(tz="UTC").isoformat()})
        st.success(f"Check queued · {jid[:10]}")
    if view == "Episodes / Data":
        data_view(ledger, lake)
    elif view == "Experiments":
        experiment_view(ledger)
    elif view == "Evidence":
        evidence_view(ledger)
    else:
        forecast_view(ledger)
