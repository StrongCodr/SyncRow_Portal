# Fix / Build List

Canonical checklist for the ingest-time analysis pipeline. Design rationale lives
in [`../RESEARCH.md`](../RESEARCH.md). Phone items are in the **SyncRow** (Android)
repo; everything else here.

## Portal analysis engine — `srow/analysis/` (built + validated on real data)
- [x] `srow/analysis/` engine (`frame`,`detect`,`resample`,`load`,`crosssensor`,`engine`,`validity`).
- [x] Synthetic axis: gravity `down` + gyro-PCA `sweep`, feather rejection, deterministic sign, projected signed 1-D, z-score. **Validated: ~90% variance-explained.**
- [x] Compute on RAW; per-sensor rate estimate; **streaming loader** (fixed OOM) + uniform resample.
- [x] Running-median crossing + sub-sample catch timing (hysteresis + refractory → 1 catch/stroke).
- [x] Cross-correlation lag (Gaussian interp) — **per-stroke windowed x-corr is the primary offset estimator**.
- [x] Cross-sensor sign alignment (flip anti-phase seats before matching).
- [x] Per-crossing timing uncertainty (true high-freq noise / slope).
- [x] Reference = stroke seat (highest index); signed per-seat offset + crew spread. **Two estimators agree.**
- [x] Allow N≥2 (rowing sensors only; cox auto-excluded via stroke-band check).
- [x] keep-and-flag: `drill` / `out_of_range` / `reference_degraded` per stroke (stroke-wide); excluded from aggregates, not dropped.
- [x] **Per-seat, per-stroke `seat_status`** (`ok`/`degraded_signal`/`low_confidence`/`reference_bad`): one spaced-out seat never voids the boat's stroke. Acquisition (held-run) checked before match quality, so "sensor spaced out" ≠ "rower spaced out". Aggregates use each seat's OK strokes independently. Real interval: 579/629 strokes keep ≥2 usable seats.
- [x] `degraded_signal` = zero-order-hold detector (`SensorFrame.held` on raw gyro; longest run > `max_held_run_frac` of the stroke) — the [[sensor-held-samples]] failure mode, named distinctly from a genuine waveform mismatch.
- [x] Confidence + uncertainty from the x-corr peak ρ (same estimator that sets the offset), not catch-slope; `min_corr` gate.
- [x] Per-seat offset uncertainty (CRB-style from ρ + window); stop reporting sub-jitter precision.
- [x] All magic numbers in `config.AnalysisConfig`; local per-stroke window (rate-change robustness).
- [x] Tests: `tests/test_analysis.py` — gaussian_lag sign, axis recovery, non-rowing exclusion, and **synthetic ground-truth** (recovers injected offsets ±sign).
- [ ] **HARDWARE ground-truth bench test** (two sensors tapped together / known injected delay) — synthetic proves the math; real rig still unproven.
- [ ] Validate across ALL 7 intervals + a 2× (2-sensor) case, not just one.
- [ ] Retire `analyze_async` in favor of the engine (CLI wrapper).
- [ ] Demote `1/(1+spread)` to secondary series, recomputed on raw.
- [ ] (Optional) wavelet second detector.

## Pipeline / infra
- [ ] `syncrow-worker` systemd service (~2 min poll) + CLI (`--once/--force/--all`, backfills the 7).
- [ ] `analysis_state` in InfluxDB (status/version/error/fingerprint; one writer).
- [ ] Completeness: stable per-sensor counts across 2 passes; auto re-analyze on late data.
- [ ] Derived series → new InfluxDB measurements; `analysis_version` stamp (bump ⇒ re-analyze).
- [ ] API `POST /api/analyze/{intervalId}?force=1` (auth) → Re-analyze button.

## Portal dashboard (web)
- [ ] Interval states: `pending/processing/ready/failed/insufficient` (no emojis); HTMX poll while processing.
- [ ] Read derived from InfluxDB (no compute at view time).
- [ ] Stroke-rate (SPM) chart + real sync chart from derived per-stroke data.
- [ ] Raw IMU/speed/map stay display-only (200 ms agg never feeds a metric).

## SyncRow app (phone)
- [ ] Reference seat = highest index (stroke), not lowest (`StrokeAnalyzer`).
- [ ] Move to synthetic gyro-PCA axis (real-time tier); causal expanding→sliding window (~30 s).
- [ ] Quality-gated window (warm-up rejection) + display-only backfill/repaint.
- [ ] **Per-seat, per-stroke quality gating — SHARED spec with the portal.** A seat that spaces out (held/duplicated samples → `degraded_signal`) or whose waveform doesn't match (`low_confidence`) is dropped for THAT seat/stroke only; the rest of the boat and that seat's other strokes stay live. Never blank the whole crew because one seat/sensor drops. Mirror `crosssensor.seat_status` semantics (acquisition checked before match quality). See [[sensor-held-samples]].
- [ ] Confirm `seat` tag ordering unambiguous.

## Sensor / firmware — LATER (bench + app/firmware)
- [ ] Bench: RSW bit-sweep; is `0x61` fixed vs composed; `0x50` throughput cost; rate vs sensor-count; connection interval; decode a `0x50`.
- [ ] Decode `0x50` timestamp → de-jitter; per-packet sequence counter (drop detection).
- [ ] Raise rate off 50 Hz; 9-sensor plan (RF cascade / onboard logging); set RTC; mag/quaternion only if needed.
