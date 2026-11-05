"""Leakage-aware XGBoost time-series regression with traceable native artifacts."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
from xgboost import XGBRegressor

MIN_POINTS = 120
ARTIFACT_SCHEMA = 2
STRATEGIES = {
    "seasonal_persistence": "SeasonalPersistence",
    "xgb_residual": "XGBoostResidualRegressor",
    "xgb_absolute": "XGBoostRegressor",
}


def _series(observations: list[dict], *, minimum: int = MIN_POINTS) -> tuple[pd.Series, pd.Series, dict]:
    if len(observations) < minimum:
        raise ValueError(f"At least {minimum} actual observations are required")
    identities = {(item.get("region", ""), item.get("metric", ""), item.get("unit", "")) for item in observations}
    if len(identities) != 1:
        raise ValueError("Select one region, metric and unit before training or predicting")
    identity = {}
    for key in ("source_id", "region_id", "series_key"):
        values = {item.get(key) for item in observations}
        if len(values) > 1:
            raise ValueError(f"Select one {key} before training or predicting")
        if next(iter(values)) is not None:
            identity[key] = next(iter(values))
    records = []
    for item in observations:
        try:
            value = float(item["value"])
            timestamp = pd.Timestamp(item["observed_at"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Each observation needs observed_at and numeric value") from exc
        if not math.isfinite(value) or value < 0 or pd.isna(timestamp):
            raise ValueError("Training values must be finite, nonnegative observations")
        if item.get("quality_status", item.get("quality")) in {"simulated", "modelled", "predicted", "forecast"} or item.get("kind") in {"simulated", "predicted"}:
            raise ValueError("Predictions and simulation values cannot be used as actual training labels")
        timestamp = timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
        records.append((timestamp, value))
    records.sort(key=lambda item: item[0])
    index = pd.DatetimeIndex([item[0] for item in records])
    if index.has_duplicates:
        raise ValueError("Duplicate timestamps must be resolved before model training")
    differences = np.diff(index.asi8) / 1e9
    seconds = int(np.median(differences))
    if seconds <= 0 or seconds > 7 * 86400:
        raise ValueError("Unsupported observation frequency")
    offsets = (index.asi8 - index.asi8[0]) / (seconds * 1e9)
    if not np.allclose(offsets, np.round(offsets), atol=1e-6):
        raise ValueError("Irregular timestamps do not align to an inferred observation frequency")
    full_index = pd.date_range(index[0], index[-1], freq=pd.Timedelta(seconds=seconds))
    if len(full_index) > 2 * len(index):
        raise ValueError("More than half the expected observations are missing")
    actual = pd.Series([item[1] for item in records], index=index, dtype=float).reindex(full_index)
    observed_mask = actual.notna()
    # Only past known values may fill history features. Missing target days are
    # never treated as measurements or scored as evaluation labels.
    history = actual.ffill()
    region, metric, unit = next(iter(identities))
    calendar_timezone = "Asia/Kolkata" if region == "Karnataka" else "Europe/London" if region.startswith("London/") else "UTC"
    return history, observed_mask, {"frequency_seconds": seconds, "missing_periods": int((~observed_mask).sum()),
                                    "calendar_timezone": calendar_timezone,
                                    "region": region, "metric": metric, "unit": unit, **identity}


def _calendar(index: pd.DatetimeIndex, calendar_timezone: str = "UTC") -> pd.DataFrame:
    # Deterministic calendar information is known for the target timestamp.
    local = index.tz_convert(calendar_timezone)
    hour = local.hour + local.minute / 60
    return pd.DataFrame({
        "dow_sin": np.sin(2 * np.pi * local.dayofweek / 7),
        "dow_cos": np.cos(2 * np.pi * local.dayofweek / 7),
        "year_sin": np.sin(2 * np.pi * local.dayofyear / 365.25),
        "year_cos": np.cos(2 * np.pi * local.dayofyear / 365.25),
        "hour_sin": np.sin(2 * np.pi * hour / 24),
        "hour_cos": np.cos(2 * np.pi * hour / 24),
    }, index=index)


def _features(history: pd.Series, lags: list[int], windows: list[int], calendar_timezone: str = "UTC") -> pd.DataFrame:
    frame = _calendar(history.index, calendar_timezone)
    for lag in lags:
        frame[f"lag_{lag}"] = history.shift(lag)
    prior = history.shift(1)
    for window in windows:
        frame[f"rolling_mean_{window}"] = prior.rolling(window).mean()
        frame[f"rolling_std_{window}"] = prior.rolling(window).std(ddof=0)
    return frame


def _errors(actual: np.ndarray, predictions: np.ndarray) -> dict:
    residual = np.asarray(actual) - np.asarray(predictions)
    return {"mae": float(np.abs(residual).mean()), "rmse": float(np.sqrt(np.square(residual).mean()))}


def _estimator() -> XGBRegressor:
    return XGBRegressor(n_estimators=180, max_depth=3, learning_rate=.045,
        objective="reg:squarederror", subsample=.9, colsample_bytree=.9,
        reg_lambda=5, random_state=42, n_jobs=1, tree_method="hist")


def _interval_radius(actual: np.ndarray, predictions: np.ndarray) -> float:
    residuals = np.abs(np.asarray(actual) - predictions)
    quantile_level = min(1.0, math.ceil((len(residuals) + 1) * .9) / len(residuals))
    return float(np.quantile(residuals, quantile_level, method="higher"))


def _select_champion(validation_scores: dict) -> str:
    # Stable ordering gives persistence priority on ties. Final test scores never
    # enter selection, estimator fitting, or interval calibration.
    return min(STRATEGIES, key=lambda key: validation_scores[key]["mae"])


def train_forecaster(observations: list[dict], artifact_dir: Path, target: str = "demand") -> dict:
    """Fit on 70%, select/calibrate on 15%, then score untouched final 15%.

    Metrics are rolling-origin ONE-STEP forecasts using available past actuals.
    The trained estimator remains fixed during calibration and testing. Future
    inference is recursive, so its long-horizon quality is not implied by these
    one-step scores. No future weather or future target enters a feature.
    """
    history, mask, context = _series(observations)
    metric = context["metric"]
    if target in {"solar", "wind", "solar_generation", "wind_generation"} and target not in metric:
        raise ValueError(f"{target} forecasting is disabled: actual {target} target labels are required")
    if metric and target == "demand":
        target = metric
    elif metric and target != metric and target not in {"solar", "wind"}:
        raise ValueError(f"Requested target {target!r} differs from observation metric {metric!r}")
    frequency = context["frequency_seconds"]
    # One week for daily input, one day for subdaily input. Keep the maximum
    # lag supportable for a bounded household subset.
    period = 7 if frequency >= 86400 else max(1, round(86400 / frequency))
    if period > len(history) // 4:
        period = 1
    lags = sorted({1, 2, period, min(2 * period, 28 if frequency >= 86400 else 96)})
    windows = sorted({3, min(period, 28)})
    features = _features(history, lags, windows, context["calendar_timezone"])
    valid = mask & features.notna().all(axis=1)
    features, labels = features.loc[valid], history.loc[valid]
    if len(features) < 90:
        raise ValueError("Insufficient actual labels after lag construction; import more observations")
    train_end, validation_end = int(len(features) * .70), int(len(features) * .85)
    x_train, y_train = features.iloc[:train_end], labels.iloc[:train_end]
    x_validation, y_validation = features.iloc[train_end:validation_end], labels.iloc[train_end:validation_end]
    x_test, y_test = features.iloc[validation_end:], labels.iloc[validation_end:]
    persistence = history.shift(period)
    absolute_model, residual_model = _estimator(), _estimator()
    absolute_model.fit(x_train, y_train)
    residual_model.fit(x_train, y_train - persistence.loc[y_train.index])
    validation_predictions = {
        "seasonal_persistence": persistence.loc[y_validation.index].to_numpy(),
        "xgb_absolute": np.maximum(0, absolute_model.predict(x_validation)),
        "xgb_residual": np.maximum(0, persistence.loc[y_validation.index].to_numpy() + residual_model.predict(x_validation)),
    }
    validation_scores = {key: _errors(y_validation.to_numpy(), values) for key, values in validation_predictions.items()}
    champion = _select_champion(validation_scores)
    selection_reason = (f"Lowest chronological validation MAE: {champion} "
                        f"({validation_scores[champion]['mae']:.6g} {context['unit']}); "
                        "persistence wins ties. Selection frozen before final-test scoring.")
    # All final-test predictions are computed only after the selection is frozen.
    test_predictions = {
        "seasonal_persistence": persistence.loc[y_test.index].to_numpy(),
        "xgb_absolute": np.maximum(0, absolute_model.predict(x_test)),
        "xgb_residual": np.maximum(0, persistence.loc[y_test.index].to_numpy() + residual_model.predict(x_test)),
    }
    radii = {key: _interval_radius(y_validation.to_numpy(), values) for key, values in validation_predictions.items()}
    candidate_scores = {key: {
        "algorithm": STRATEGIES[key], "selected": key == champion,
        "validation_mae": validation_scores[key]["mae"], "validation_rmse": validation_scores[key]["rmse"],
        **_errors(y_test.to_numpy(), values), "interval_radius": radii[key],
        "interval_coverage": float((np.abs(y_test.to_numpy() - values) <= radii[key]).mean()),
    } for key, values in test_predictions.items()}
    radius = radii[champion]
    selected_predictions = test_predictions[champion]
    scores = _errors(y_test.to_numpy(), selected_predictions)
    baseline_scores = candidate_scores["seasonal_persistence"]
    metrics = {
        **scores, "baseline_mae": baseline_scores["mae"], "baseline_rmse": baseline_scores["rmse"],
        "validation_mae": validation_scores[champion]["mae"], "validation_rmse": validation_scores[champion]["rmse"],
        "baseline_validation_mae": validation_scores["seasonal_persistence"]["mae"],
        "interval_coverage": candidate_scores[champion]["interval_coverage"],
        "interval_nominal_coverage": .9, "interval_radius": radius,
        "train_samples": len(y_train), "validation_samples": len(y_validation), "test_samples": len(y_test),
        "evaluation": "chronological rolling-origin one-step", "baseline": f"lag-{period} seasonal persistence",
        "outperforms_baseline_mae": scores["mae"] < baseline_scores["mae"],
        "champion": champion, "champion_algorithm": STRATEGIES[champion],
        "selection_metric": "validation_mae", "selection_reason": selection_reason,
        "candidates": candidate_scores,
    }
    if np.all(y_test.to_numpy() > 1e-6):
        metrics["mape_percent"] = float(np.mean(np.abs((y_test.to_numpy() - selected_predictions) / y_test.to_numpy())) * 100)
    model_id = str(uuid4())
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + model_id[:8]
    folder = Path(artifact_dir).resolve() / model_id
    folder.mkdir(parents=True, exist_ok=False)
    metadata_path = folder / "metadata.json"
    model_files = {"xgb_absolute": "model.json", "xgb_residual": "residual_model.json"}
    absolute_model.save_model(str(folder / model_files["xgb_absolute"]))
    residual_model.save_model(str(folder / model_files["xgb_residual"]))
    artifact_files = {key: {"file": filename, "sha256": hashlib.sha256((folder / filename).read_bytes()).hexdigest()}
                      for key, filename in model_files.items()}
    # Keep the legacy primary-file fields for tooling which inspects artifacts.
    primary = artifact_files[champion if champion in model_files else "xgb_residual"]
    digest_input = "\n".join(f"{timestamp.isoformat()}|{value:.12g}|{int(mask.loc[timestamp])}" for timestamp, value in history.items())
    metadata = {
        "id": model_id, "name": f"{STRATEGIES[champion]} {target}", "version": version,
        "algorithm": STRATEGIES[champion], "target": target, "metrics": metrics,
        "prediction_strategy": champion, "seasonal_period": period, "selection_reason": selection_reason,
        "training_start": y_train.index[0].isoformat(), "training_end": y_train.index[-1].isoformat(),
        "validation_start": y_validation.index[0].isoformat(), "validation_end": y_validation.index[-1].isoformat(),
        "test_start": y_test.index[0].isoformat(), "test_end": y_test.index[-1].isoformat(),
        "history_start": history.index[0].isoformat(), "history_end": history.index[-1].isoformat(),
        "trained_at": datetime.now(timezone.utc).isoformat(), "artifact_path": str(metadata_path),
        "model_file": primary["file"], "artifact_schema": ARTIFACT_SCHEMA,
        "model_sha256": primary["sha256"], "candidate_artifacts": artifact_files,
        "training_data_sha256": hashlib.sha256(digest_input.encode()).hexdigest(),
        "source_ids": sorted({str(item.get("source_id", "")) for item in observations}),
        "observation_ids": [str(item.get("id", "")) for item in observations],
        "feature_names": list(features.columns), "lags": lags, "windows": windows, **context,
        "interval_method": "90% absolute residual quantile on heldout chronological validation; sqrt(horizon) widening is heuristic",
        "limitations": ["One-step test scores do not validate recursive multi-step performance.",
            "Time-series dependence and changing conditions can reduce interval coverage.",
            "Validation is reused for candidate selection and interval calibration; nominal coverage is descriptive, not guaranteed.",
            "Missing history periods are forward-filled for features only; never scored as measured labels.",
            "No future weather data, renewable labels or failure labels are invented."],
        "test_predictions": [{"target_at": date.isoformat(), "actual": float(actual), "value": float(value),
            "lower": max(0.0, float(value) - radius), "upper": float(value) + radius}
            for date, actual, value in zip(y_test.index, y_test, selected_predictions)][-336:],
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, allow_nan=False), encoding="utf-8")
    return metadata


def load_predict(artifact_path: str, observations: list[dict], horizon: int = 14) -> list[dict]:
    """Load a native JSON artifact and recursively forecast in original units."""
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 336:
        raise ValueError("horizon must be an integer between 1 and 336 observation periods")
    path = Path(artifact_path).resolve()
    if path.is_dir():
        path = path / "metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    schema = metadata.get("artifact_schema")
    if schema not in {1, ARTIFACT_SCHEMA}:
        raise ValueError("Unsupported forecast artifact schema")
    artifacts = list(metadata.get("candidate_artifacts", {}).values()) if schema == 2 else []
    artifacts.append({"file": metadata["model_file"], "sha256": metadata["model_sha256"]})
    for artifact in artifacts:
        model_path = path.parent / artifact["file"]
        if model_path.resolve().parent != path.parent:
            raise ValueError("Invalid model artifact path")
        if hashlib.sha256(model_path.read_bytes()).hexdigest() != artifact["sha256"]:
            raise ValueError("Model artifact checksum verification failed")
    minimum = max(max(metadata["lags"]), max(metadata["windows"])) + 1
    history, _, context = _series(observations, minimum=minimum)
    for key in ("frequency_seconds", "region", "metric", "unit"):
        if context[key] != metadata[key]:
            raise ValueError(f"Prediction observations have a different {key} from the trained model")
    for key in ("source_id", "region_id", "series_key"):
        if key in metadata and context.get(key) != metadata[key]:
            raise ValueError(f"Prediction observations have a different {key} from the trained model")
    strategy = metadata.get("prediction_strategy", "xgb_absolute") if schema == 2 else "xgb_absolute"
    if strategy not in STRATEGIES:
        raise ValueError("Unsupported forecast prediction strategy")
    model = None
    if strategy != "seasonal_persistence":
        model = XGBRegressor()
        model.load_model(str(path.parent / metadata["model_file"]))
    predictions = []
    for step in range(1, horizon + 1):
        timestamp = history.index[-1] + pd.Timedelta(seconds=context["frequency_seconds"])
        history.loc[timestamp] = np.nan
        frame = _features(history, metadata["lags"], metadata["windows"], metadata.get("calendar_timezone", "UTC")).iloc[[-1]]
        seasonal_value = float(history.iloc[-1 - metadata.get("seasonal_period", 1)])
        if strategy == "seasonal_persistence":
            value = seasonal_value
        else:
            value = float(model.predict(frame[metadata["feature_names"]])[0])
            if strategy == "xgb_residual":
                value += seasonal_value
        if not math.isfinite(value):
            raise ValueError("Model returned a nonfinite prediction")
        value = max(0.0, value)
        history.loc[timestamp] = value
        radius = metadata["metrics"]["interval_radius"] * math.sqrt(step)
        predictions.append({"target_at": timestamp.isoformat(), "value": value,
                            "lower": max(0.0, value - radius), "upper": value + radius})
    return predictions
