"""Fixed chronological benchmark. This is research, never automatic promotion."""
import platform
import hashlib
from pathlib import Path
import numpy as np
import pandas as pd
import sklearn
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from tesla_lab.calendar import cutoff, exchange, sessions
from tesla_lab.data import Lake
from tesla_lab.episodes import get_review, make_labels
from tesla_lab.ledger import identity
from tesla_lab.worker import Blocked


FEATURES = ["return_1", "return_5", "return_20", "volatility_20", "relative_volume_5",
            "distance_high_20", "trend_consistency_5", "relative_strength_20"]
PROTOCOL = "daily-baseline-v1"


def source_version():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for file in sorted(root.glob("*.py")):
        digest.update(file.name.encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


def build_features(frame, enforce_availability=False):
    if not {"TSLA", "SPY"} <= set(frame.symbol):
        raise Blocked("Daily baseline requires TSLA and SPY in the same dataset version")
    index = sessions(frame.session.min(), frame.session.max())
    groups = {}
    valid = pd.Series(True, index=index)
    cutoff_seconds = pd.Series([cutoff(d).timestamp() for d in index], index=index)
    for symbol in ("TSLA", "SPY"):
        g = frame.loc[frame.symbol == symbol].copy()
        g.index = pd.to_datetime(g.session)
        g = g.reindex(index)
        groups[symbol] = g
        # Every feature context requires 21 actual consecutive exchange observations.
        valid &= g.close.notna().rolling(21, min_periods=21).sum().shift(1).eq(21)
        observed = pd.to_datetime(g.observed_at, utc=True).map(lambda t: t.timestamp() if pd.notna(t) else np.nan)
        valid &= observed.rolling(21, min_periods=21).max().shift(1).lt(cutoff_seconds)
        if enforce_availability:
            available = pd.to_datetime(g.available_at, utc=True).map(lambda t: t.timestamp() if pd.notna(t) else np.nan)
            valid &= available.rolling(21, min_periods=21).max().shift(1).le(cutoff_seconds)
    stock, market = groups["TSLA"], groups["SPY"]
    ret = stock.close.pct_change(fill_method=None)
    f = pd.DataFrame(index=index)
    for days in (1, 5, 20):
        f[f"return_{days}"] = stock.close.pct_change(days, fill_method=None).shift(1)
    f["volatility_20"] = np.log(stock.close).diff().rolling(20).std().shift(1)
    f["relative_volume_5"] = (stock.volume.rolling(5).mean() / stock.volume.rolling(20).mean()).shift(1)
    f["distance_high_20"] = (stock.close / stock.high.rolling(20).max() - 1).shift(1)
    f["trend_consistency_5"] = ret.gt(0).astype(float).where(ret.notna()).rolling(5).mean().shift(1)
    f["relative_strength_20"] = (stock.close.pct_change(20, fill_method=None) - market.close.pct_change(20, fill_method=None)).shift(1)
    f = f.replace([np.inf, -np.inf], np.nan)
    f.loc[~valid] = np.nan
    return f[FEATURES]


def purged_before(frame, boundary):
    return frame.loc[(frame.index < boundary) & (frame.label_end < boundary)]


def learner(c):
    return make_pipeline(StandardScaler(), LogisticRegression(C=c, max_iter=1000, solver="lbfgs", random_state=0))


def alerts(scores, threshold, positions, cooldown=10):
    selected = np.zeros(len(scores), dtype=bool)
    last = -100000
    if threshold is None:
        return selected
    for n, (score, position) in enumerate(zip(scores, positions)):
        if score >= threshold and position - last >= cooldown:
            selected[n] = True
            last = position
    return selected


def validation_threshold(scores, positions, budget):
    span = int(positions[-1] - positions[0] + 1)
    allowed = int(np.floor(budget * span / 252))
    choices = np.unique(np.quantile(scores, np.linspace(0, 1, 51)))
    feasible = [float(t) for t in choices if alerts(scores, t, positions).sum() <= allowed]
    return min(feasible) if feasible else None


def event_metrics(predictions, review, index, horizon):
    positions = {d.date().isoformat(): n for n, d in enumerate(index)}
    onsets = {e["onset"] for e in review["episodes"]}
    eligible = {onset for onset in onsets if onset in positions and any(
        0 <= positions[onset] - p["position"] < horizon for p in predictions)}
    detected, leads, false_alerts, count = set(), [], 0, 0
    for p in predictions:
        if not p["alert"]:
            continue
        count += 1
        matches = sorted(o for o in eligible if o not in detected and 0 <= positions[o] - p["position"] < horizon)
        if matches:
            onset = matches[0]
            detected.add(onset)
            leads.append(positions[onset] - p["position"])
        else:
            false_alerts += 1
    return {"alerts": count, "false_alerts": false_alerts, "onsets_detected": len(detected),
            "eligible_onsets": len(eligible), "precision": len(detected) / count if count else None,
            "episode_recall": len(detected) / len(eligible) if eligible else None,
            "lead_sessions": leads, "same_session_preopen_warnings": sum(x == 0 for x in leads)}


def run_experiment(ledger, configuration):
    config = dict(configuration)
    if not config.get("review_id"):
        raise Blocked("A complete price-only review version is required; unreviewed dates are not negatives")
    if config.get("protocol", PROTOCOL) != PROTOCOL:
        raise Blocked("Only the frozen daily-baseline-v1 protocol is implemented")
    horizon = config.get("horizon", 10)
    budget = config.get("alert_budget", 4)
    if horizon not in (5, 10) or budget not in (4, 8):
        raise ValueError("Unsupported horizon or alert budget")
    lake = Lake(ledger)
    info = lake.info(config["dataset_id"])
    if info["kind"] != "equity_daily" or info["metadata"].get("price_basis") != "split_adjusted":
        raise Blocked("Baseline requires split-adjusted daily equity data")
    review = get_review(ledger, config["review_id"])
    data = lake.read(config["dataset_id"])
    timing = config.get("timing_mode", "retrospective")
    if timing not in {"retrospective", "point_in_time"}:
        raise ValueError("Unknown timing mode")
    if timing == "point_in_time" and info["metadata"]["availability"] != "verified":
        raise Blocked("Point-in-time evaluation requires verified availability clocks")
    features = build_features(data, enforce_availability=timing == "point_in_time")
    index = features.index
    labels = make_labels(index, review, horizon)
    sample = features.join(labels).dropna(subset=FEATURES + ["target", "label_end"])
    if sample.empty:
        raise Blocked("No complete features and mature reviewed outcomes are available")
    if sample.target.nunique() < 2:
        raise Blocked("Reviewed history needs both onset and non-onset outcomes")
    test_start = index[0] + pd.DateOffset(years=3)
    predictions, folds, trials = [], [], []
    while test_start <= index[-1]:
        test_end = test_start + pd.DateOffset(months=6)
        validation_start = test_start - pd.DateOffset(months=6)
        inner = purged_before(sample, validation_start)
        validation = sample.loc[(sample.index >= validation_start) & (sample.index < test_start) & (sample.label_end < test_start)]
        train = purged_before(sample, test_start)
        test = sample.loc[(sample.index >= test_start) & (sample.index < test_end)]
        if len(inner) < 100 or inner.target.nunique() < 2 or len(validation) < 20 or test.empty:
            folds.append({"test_start": test_start.date().isoformat(), "status": "blocked_sparse_or_unreviewed_fold"})
            test_start = test_end
            continue
        candidates = []
        for c in (.1, 1., 10.):
            model = learner(c).fit(inner[FEATURES], inner.target)
            scores = model.predict_proba(validation[FEATURES])[:, 1]
            loss = float(log_loss(validation.target, scores, labels=[0, 1]))
            trials.append({"test_start": test_start.date().isoformat(), "C": c, "validation_log_loss": loss})
            candidates.append((loss, c, scores))
        _, c, validation_scores = min(candidates, key=lambda x: (x[0], x[1]))
        positions = index.get_indexer(validation.index)
        threshold = validation_threshold(validation_scores, positions, budget)
        model = learner(c).fit(train[FEATURES], train.target)
        probability = model.predict_proba(test[FEATURES])[:, 1]
        base = float(train.target.mean())
        test_positions = index.get_indexer(test.index)
        # Carry cooldown across outer blocks; fold-specific thresholds remain frozen.
        previous_position = max((p["position"] for p in predictions if p["alert"]), default=-100000)
        selected = alerts(probability, threshold, test_positions)
        if previous_position > -100000:
            selected[test_positions - previous_position < 10] = False
            # Re-run policy sequentially with inherited state, rather than deleting
            # early picks which could suppress a later feasible alert.
            for n in range(len(selected)):
                selected[n] = threshold is not None and probability[n] >= threshold and test_positions[n] - previous_position >= 10
                if selected[n]:
                    previous_position = test_positions[n]
        for n, (date, row) in enumerate(test.iterrows()):
            predictions.append({"session": date.date().isoformat(), "position": int(test_positions[n]),
                "probability": float(probability[n]), "baseline_probability": base, "target": int(row.target),
                "label_end": row.label_end.date().isoformat(), "alert": bool(selected[n]),
                "threshold": threshold, "cutoff": cutoff(date).isoformat()})
        folds.append({"test_start": test_start.date().isoformat(), "test_end": test_end.date().isoformat(),
            "status": "evaluated", "training_rows": len(train), "validation_rows": len(validation),
            "test_rows": len(test), "C": c, "threshold": threshold, "max_train_label_end": train.label_end.max().date().isoformat()})
        test_start = test_end
    if not predictions:
        raise Blocked("Need at least three years of usable history and complete chronological review for an evaluable outer fold")
    y = [p["target"] for p in predictions]
    probabilities = [p["probability"] for p in predictions]
    base = [p["baseline_probability"] for p in predictions]
    metrics = {"predictions": len(y), "positive_dates": sum(y), "log_loss": float(log_loss(y, probabilities, labels=[0, 1])),
        "baseline_log_loss": float(log_loss(y, base, labels=[0, 1])), "brier": float(brier_score_loss(y, probabilities)),
        "baseline_brier": float(brier_score_loss(y, base))} | event_metrics(predictions, review, index, horizon)
    calibration = []
    for lower, upper in zip((0., .05, .1, .2, .5), (.05, .1, .2, .5, 1.)):
        chosen = [p for p in predictions if lower <= p["probability"] and (p["probability"] < upper or upper == 1.)]
        calibration.append({"lower": lower, "upper": upper, "count": len(chosen),
            "positive_dates": sum(p["target"] for p in chosen),
            "mean_probability": float(np.mean([p["probability"] for p in chosen])) if chosen else None,
            "observed_rate": float(np.mean([p["target"] for p in chosen])) if chosen else None})
    report = {"protocol": PROTOCOL, "configuration": config, "configuration_hash": identity(config), "code_version": source_version(),
        "dataset": info, "review_version": config["review_id"], "features": FEATURES,
        "evidence": "retrospective_experimental", "metrics": metrics, "folds": folds, "trials": trials,
        "predictions": predictions, "calibration": calibration,
        "runtime": {"python": platform.python_version(), "sklearn": sklearn.__version__, "pandas": pd.__version__},
        "limitations": ["not_causal", "known_examples_previously_inspected", "no_production_promotion",
                        "no_uncertainty_intervals_yet", "fixed_daily_benchmark_only", "gamma_flow_mechanism_tests_pending"]}
    if timing == "retrospective":
        report["limitations"].append("historical_publication_and_revision_availability_not_proven")
    rel = lake.report(report)
    return {"report_path": rel, "configuration_hash": identity(config), "evidence": report["evidence"], "metrics": metrics}
