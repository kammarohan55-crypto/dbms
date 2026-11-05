"""Forecast chronology, artifact integrity, and truthful data ingestion checks."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gridmind.forecasting import _features, STRATEGIES, load_predict, train_forecaster
from gridmind.public_data import fetch_karnataka, parse_csv


def observations(count=220, seconds=86400):
    start = datetime(2022, 1, 1, tzinfo=timezone.utc)
    return [{"id": str(i), "source_id": "test-observed", "region": "Test", "metric": "load_kwh", "unit": "kWh",
             "observed_at": (start + timedelta(seconds=i * seconds)).isoformat(),
             "value": 50 + 4 * np.sin(i * 2 * np.pi / 7) + i * .03, "quality": "observed"}
            for i in range(count)]


def test_shifted_features_do_not_see_target_or_future():
    series = pd.Series(np.arange(40, dtype=float), index=pd.date_range("2024-01-01", periods=40, tz="UTC"))
    before = _features(series, [1, 7], [3, 7])
    altered = series.copy()
    altered.iloc[20:] = 999999
    after = _features(altered, [1, 7], [3, 7])
    pd.testing.assert_series_equal(before.iloc[20], after.iloc[20])
    assert before.iloc[20]["lag_1"] == 19
    assert before.iloc[20]["rolling_mean_3"] == 18


def test_forecast_native_artifact_roundtrip_and_chronology(tmp_path):
    readings = observations()
    metadata = train_forecaster(readings, tmp_path)
    assert metadata["artifact_schema"] == 2
    assert metadata["algorithm"] == STRATEGIES[metadata["prediction_strategy"]]
    assert metadata["target"] == "load_kwh"
    assert metadata["training_end"] < metadata["validation_start"] < metadata["test_start"]
    assert metadata["metrics"]["mae"] >= 0
    assert metadata["metrics"]["baseline_rmse"] >= 0
    assert 0 <= metadata["metrics"]["interval_coverage"] <= 1
    assert metadata["training_data_sha256"]
    assert metadata["metrics"]["selection_metric"] == "validation_mae"
    candidates = metadata["metrics"]["candidates"]
    assert set(candidates) == set(STRATEGIES)
    champion = metadata["prediction_strategy"]
    assert candidates[champion]["validation_mae"] == min(candidate["validation_mae"] for candidate in candidates.values())
    assert metadata["metrics"]["mae"] == candidates[champion]["mae"]
    assert all(candidate["rmse"] >= 0 and 0 <= candidate["interval_coverage"] <= 1 for candidate in candidates.values())
    first = load_predict(metadata["artifact_path"], readings, 3)
    second = load_predict(metadata["artifact_path"], readings, 3)
    assert first == second
    assert len(first) == 3
    assert first[0]["target_at"] > readings[-1]["observed_at"]
    assert all(0 <= row["lower"] <= row["value"] <= row["upper"] for row in first)
    assert pd.Timestamp(first[1]["target_at"]) - pd.Timestamp(first[0]["target_at"]) == pd.Timedelta(days=1)
    model_path = Path(metadata["artifact_path"]).parent / "model.json"
    model_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        load_predict(metadata["artifact_path"], readings)


def test_small_half_hour_subset_keeps_frequency(tmp_path):
    readings = observations(220, 1800)
    metadata = train_forecaster(readings, tmp_path)
    result = load_predict(metadata["artifact_path"], readings, 2)
    assert metadata["frequency_seconds"] == 1800
    assert pd.Timestamp(result[1]["target_at"]) - pd.Timestamp(result[0]["target_at"]) == pd.Timedelta(minutes=30)


def test_test_labels_cannot_change_selection_models_or_interval_calibration(tmp_path):
    readings = observations()
    first = train_forecaster(readings, tmp_path)
    changed = [dict(row) for row in readings]
    for row in changed:
        if row['observed_at'] >= first['test_start']:
            row['value'] += 1000
    second = train_forecaster(changed, tmp_path)
    assert first['prediction_strategy'] == second['prediction_strategy']
    assert first['candidate_artifacts'] == second['candidate_artifacts']
    assert first['metrics']['validation_mae'] == second['metrics']['validation_mae']
    assert first['metrics']['interval_radius'] == second['metrics']['interval_radius']
    assert first['metrics']['mae'] != second['metrics']['mae']


def test_persistence_champion_is_the_estimator_used_for_future_predictions(tmp_path):
    readings = observations()
    for index, row in enumerate(readings):
        row['value'] = float(10 + index % 7)
        row['series_key'] = 'meter-A'
    metadata = train_forecaster(readings, tmp_path)
    assert metadata['prediction_strategy'] == 'seasonal_persistence'
    assert metadata['algorithm'] == 'SeasonalPersistence'
    assert metadata['metrics']['mae'] == metadata['metrics']['baseline_mae'] == 0
    assert metadata['series_key'] == 'meter-A'
    stored = json.loads(Path(metadata['artifact_path']).read_text())
    assert stored['series_key'] == 'meter-A'
    result = load_predict(metadata['artifact_path'], readings, 14)
    assert [row['value'] for row in result] == [row['value'] for row in readings[-7:]] * 2
    wrong_series = [{**row, 'series_key':'meter-B'} for row in readings]
    with pytest.raises(ValueError, match='different series_key'):
        load_predict(metadata['artifact_path'], wrong_series, 1)


def test_version_one_native_artifact_still_loads_as_absolute_xgboost(tmp_path):
    from xgboost import XGBRegressor
    readings = observations()
    metadata = train_forecaster(readings, tmp_path)
    path = Path(metadata['artifact_path'])
    metadata['artifact_schema'] = 1
    metadata['model_file'] = 'model.json'
    metadata['model_sha256'] = hashlib.sha256((path.parent / 'model.json').read_bytes()).hexdigest()
    # v1 prediction must remain absolute even if unknown newer keys are present.
    metadata['prediction_strategy'] = 'seasonal_persistence'
    path.write_text(json.dumps(metadata), encoding='utf-8')
    result = load_predict(str(path), readings, 1)
    history = pd.Series([row['value'] for row in readings], index=pd.to_datetime([row['observed_at'] for row in readings]))
    history.loc[history.index[-1] + pd.Timedelta(days=1)] = np.nan
    frame = _features(history, metadata['lags'], metadata['windows']).iloc[[-1]]
    estimator = XGBRegressor()
    estimator.load_model(str(path.parent / 'model.json'))
    assert result[0]['value'] == pytest.approx(float(estimator.predict(frame[metadata['feature_names']])[0]))


def test_heldout_replay_artifact_keeps_up_to_336_actual_labels(tmp_path):
    metadata = train_forecaster(observations(2400), tmp_path)
    assert metadata['metrics']['test_samples'] > 336
    assert len(metadata['test_predictions']) == 336
    assert all(row['target_at'] > metadata['validation_end'] for row in metadata['test_predictions'])


@pytest.mark.parametrize('horizon', [0,337,True,1.5])
def test_predict_rejects_invalid_horizon_before_loading(horizon):
    with pytest.raises(ValueError,match='horizon'):
        load_predict('not-a-model.json', [], horizon)


def test_missing_periods_never_become_scored_labels(tmp_path):
    readings = observations()
    missing_at = readings[150]["observed_at"]
    del readings[150]
    metadata = train_forecaster(readings, tmp_path)
    assert metadata["missing_periods"] == 1
    assert metadata["metrics"]["train_samples"] + metadata["metrics"]["validation_samples"] + metadata["metrics"]["test_samples"] < len(readings)
    assert missing_at not in {row["target_at"] for row in metadata["test_predictions"]}


def test_training_rejects_insufficient_mixed_or_simulated_labels(tmp_path):
    with pytest.raises(ValueError, match="120"):
        train_forecaster(observations(10), tmp_path)
    readings = observations()
    readings[0]["unit"] = "GWh"
    with pytest.raises(ValueError, match="one region"):
        train_forecaster(readings, tmp_path)
    readings = observations()
    readings[0]["quality"] = "simulated"
    with pytest.raises(ValueError, match="actual training"):
        train_forecaster(readings, tmp_path)
    with pytest.raises(ValueError, match="disabled"):
        train_forecaster(observations(), tmp_path, target="solar")


def test_training_rejects_duplicate_negative_and_irregular_data(tmp_path):
    readings = observations()
    readings[0]["value"] = -1
    with pytest.raises(ValueError, match="nonnegative"):
        train_forecaster(readings, tmp_path)
    readings = observations()
    readings[1]["observed_at"] = readings[0]["observed_at"]
    with pytest.raises(ValueError, match="Duplicate"):
        train_forecaster(readings, tmp_path)
    readings = observations()
    readings[1]["observed_at"] = "2022-01-02T01:00:00+00:00"
    with pytest.raises(ValueError, match="Irregular"):
        train_forecaster(readings, tmp_path)


def test_canonical_csv_deduplicates_validates_and_repeats_ids():
    csv_text = "timestamp,value,unit,metric\n2024-01-01T00:00Z,2,kWh,load\n2024-01-01T00:00Z,2,kWh,load\n2024-01-02T00:00Z,-2,kWh,load\n2024-01-03T00:00Z,NaN,kWh,load\n"
    accepted, report = parse_csv(csv_text)
    assert len(accepted) == 1
    assert report["duplicates"] == 1 and report["rejected"] == 2
    assert parse_csv(csv_text)[0][0]["id"] == accepted[0]["id"]
    assert accepted[0]["value"] == 2
    assert accepted[0]["quality"] == "observed"


def test_london_import_respects_half_hour_energy_and_dst():
    csv_text = "LCLid,stdorToU,DateTime,KWH/hh (per half hour)\nMAC000001,Std,2013-06-01 12:00:00.000,0.45\nMAC000001,Std,2013-10-27 01:00:00.000,0.4\nMAC000001,Std,2013-03-31 01:00:00.000,0.4\n"
    accepted, report = parse_csv(csv_text)
    assert report["format"] == "london_half_hourly"
    assert report["rejected"] == 2
    assert accepted[0]["observed_at"] == "2013-06-01T11:00:00+00:00"
    assert accepted[0]["region"] == "London/MAC000001"
    assert accepted[0]["value"] == .45 and accepted[0]["unit"] == "kWh"
    assert "not Karnataka" in report["geographic_scope"]


def test_csv_limit_is_explicit():
    accepted, report = parse_csv("timestamp,value,unit,metric\n2024-01-01,2,kWh,load\n2024-01-02,3,kWh,load", max_rows=1)
    assert len(accepted) == 1 and report["truncated"]


def test_cached_zenodo_data_retains_source_semantics():
    raw_dir = Path(__file__).resolve().parents[1] / "data" / "raw"
    if not (raw_dir / "corrected_daily_energy_met_MU.csv").exists():
        pytest.skip("Raw public source not downloaded; network is not required by unit tests")
    readings, source = fetch_karnataka(raw_dir)
    assert len(readings) == 3894
    assert source["metadata"]["identical_duplicate_rows"] == 49
    assert len(source["metadata"]["conflicting_dates_excluded"]) == 6
    assert source["license"] == "CC-BY-4.0"
    assert readings[0]["metric"] == "supplied_energy_gwh"
    assert readings[0]["unit"] == "GWh" and readings[0]["value"] == 171.7
    assert "not unconstrained demand" in source["metadata"]["interpretation"]
