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

## SyncRow app (phone) — repo: `../SyncRow`, branch `per-seat-quality-gating`
- [~] **Per-seat real-time quality gating — SHARED spec (done on branch, needs on-device validation).** `SensorSyncStatus {CALIBRATING, OK, DEGRADED_SIGNAL, STALE}`; `StrokeAnalyzer.onSample(…, fresh)` skips held ticks (zero-order-hold repeats) so a plateau can't corrupt the median or fake a crossing; held > 30% of a stroke → `DEGRADED_SIGNAL` (mirrors `max_held_run_frac`), acquisition checked before pairing; only an OK seat shows a lateness, degraded/stale → "--", no other seat touched. UI shows "sensor dropout". Test: a frozen seat degrades only itself, boat stays live. **No JVM/JDK in the portal env — build+run `row/app` unit tests on-device.** See [[sensor-held-samples]].
- [x] **Reference seat = highest seat number (stroke) — FIXED on branch.** `StrokeAnalyzer` referenced the LOWEST index (bow, showed 0 there) while the UI ("Stroke reference: Seat N") and portal use the highest. seatIndex already equals the user-facing "Seat N" (same formula as SensorLabelBuilder), so fix = pick MAX seatIndex; selection now by value, not add-order. Confirmed live: 0 was landing on seat 1 (bow) instead of seat 2 (stroke). Needs on-device test run.
- [x] **Double-count fix — FIXED on branch.** Schmitt trigger (band = 0.20·IQR, was ~0 hysteresis) + refractory 0.40→0.55·period; was reporting 2+ catches/stroke.
- [ ] Move to synthetic gyro-PCA axis (real-time tier); causal expanding→sliding window (~30 s).
- [ ] Quality-gated window (warm-up rejection) + display-only backfill/repaint. (`low_confidence` match-quality analogue: phone has no x-corr yet — revisit with the synthetic-axis move.)
- [ ] Confirm `seat` tag ordering unambiguous.

## Sensor / firmware — LATER (bench + app/firmware)
- [ ] Bench: RSW bit-sweep; is `0x61` fixed vs composed; `0x50` throughput cost; rate vs sensor-count; connection interval; decode a `0x50`.
- [ ] Decode `0x50` timestamp → de-jitter; per-packet sequence counter (drop detection).
- [ ] Raise rate off 50 Hz; 9-sensor plan (RF cascade / onboard logging); set RTC; mag/quaternion only if needed.
