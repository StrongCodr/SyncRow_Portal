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
- [ ] **Record frames on arrival, not on a timer (HIGH PRIORITY, 2026-10-06).** `IntervalRecordingService` writes `latestSample` for every sensor on a fixed 10 ms tick (`targetSamplePeriodMs = 10`), stamped with the tick time. Result: ~100 rows/s per sensor of which ~75 % are repeats of the previous frame, timestamps quantised to the tick instead of arrival, and a frame is silently overwritten when two notifications land inside one tick. Fix: write each 0x61 frame in `onCharacteristicChanged` with its arrival time; keep the 10 ms loop only for the live display/analyzer. Same for location: write a row only when a new fix arrives, with the fix's own time (`location.time`), and drop the 1 s dedupe re-write. Confirmed from the 3 Oct data (see `docs/FINDINGS_2026-10-05.md` §4, §6).
- [x] **Per-seat real-time quality gating (APPROXIMATION of the portal spec, not a mirror — see RESEARCH §8.5).** `SensorSyncStatus {CALIBRATING, OK, DEGRADED_SIGNAL, STALE}`; `StrokeAnalyzer.onSample(…, fresh)` skips held ticks so a plateau can't corrupt the median or fake a crossing; time-since-fresh > `HELD_RUN_FRAC` of a stroke → `DEGRADED_SIGNAL`, acquisition checked before pairing; only an OK seat shows a lateness, degraded/stale → "--", no other seat touched. UI shows "sensor dropout". **No `low_confidence` (phone has no x-corr) → phone can show a stroke the portal drops.** Tests run on a standalone JVM (kotlinc + JUnit): 4/4 pass.
- [x] **Reference seat = highest seat number (stroke) — FIXED.** `StrokeAnalyzer` referenced the LOWEST index (bow, showed 0 there) while the UI ("Stroke reference: Seat N") and portal use the highest. seatIndex already equals the user-facing "Seat N" (same formula as SensorLabelBuilder), so fix = pick MAX seatIndex; selection by value, not add-order. Confirmed live: 0 was landing on seat 1 (bow) instead of seat 2 (stroke). Test asserts it.
- [x] **Double-count fix — FIXED.** Schmitt trigger (band = 0.20·IQR, was ~0 hysteresis) + refractory 0.40→0.55·period. Catch TIME comes from the true median crossing (interpolated between straddling samples), NOT the hysteresis flip point, so timing isn't biased late. Test asserts ~1 catch/cycle on a noisy signal.
- [x] **Rate-robustness — FIXED.** Held-skip drops the detector feed from 100 Hz (poll rate) to the real BLE rate; converted the median window (`WINDOW_MS`) and gyro lookahead (`GYRO_LOOKAHEAD_MS`) from sample-count to TIME so they don't silently change span with the rate. `latestSampleMs`/`latestSample` made `@Volatile` (BLE thread → sampling loop). Degraded seats drop their stale `_lateness` from analyzer state.
- [ ] **Bigger fish: still on mount-dependent Euler channel selection.** The phone picks one of pitch/roll/yaw by variance — the approach rejected for the portal. All the gating above is correct plumbing on a suspect base; the phone's numbers aren't trustworthy until the synthetic gyro-PCA migration below. Do NOT present phone lateness as authoritative.
- [ ] Move to synthetic gyro-PCA axis (real-time tier); causal expanding→sliding window (~30 s).
- [ ] Quality-gated window (warm-up rejection) + display-only backfill/repaint. (`low_confidence` match-quality analogue: phone has no x-corr yet — revisit with the synthetic-axis move.)
- [ ] Confirm `seat` tag ordering unambiguous.
- [ ] **Experiment: sensor TIME packet (0x50) for true sample times (2026-10-06).** Goal: record each frame's time as the sensor saw it, as exactly and realistically as possible, alongside the phone arrival time. Steps: (1) enable the existing `enableTimePacket` toggle for one session (writes RSW bit 0); (2) decode the 0x50 payload in `onCharacteristicChanged` instead of just counting it (Y/M/D/h/m/s/ms, 10 bytes); (3) store `sensor_time_ms` on every row next to arrival time — pair each 0x61 frame with the most recent 0x50 and the frame count since it; (4) measure the cost: does the extra stream reduce 0x61 delivery below ~25/s (compare `imu_diag.received` with and without); (5) in the portal, fit sensor time vs arrival time per sensor (robust linear fit) and use it for all timing. If the TIME stream costs too much bandwidth, fall back to a per-frame sequence counter if the firmware offers one, else the arrival-cadence clock recovery (FINDINGS §5.3 item 1).

## Sensor / firmware — LATER (bench + app/firmware)
- [ ] Bench: RSW bit-sweep; is `0x61` fixed vs composed; `0x50` throughput cost; rate vs sensor-count; connection interval; decode a `0x50`.
- [ ] Decode `0x50` timestamp → de-jitter; per-packet sequence counter (drop detection).
- [ ] Raise rate off 50 Hz; 9-sensor plan (RF cascade / onboard logging); set RTC; mag/quaternion only if needed.

## 2026-10-10 engine: sweep axis fixed (feather flip, gravity projection, hole gating)
- [x] Feather-flip axis detected explicitly (PC1 of > 2 Hz gyro, p99 >= 60 deg/s) and removed
      before the sweep PCA; the PCA runs on the < 2.5 Hz content. The PC1-out-of-band -> PC2
      fallback is gone (every axis on an oar is stroke-periodic, so it only produced 90 deg jumps).
- [x] Sweep axis no longer orthogonalised against gravity. On a scull the pin axis is 8-25 deg
      from `down`; the projection left wobble, hence zero correlation with the real sweep.
- [x] Axis window 10 s -> 60 s (10 s broke on BLE-holey data); held rows interpolated; projected
      signal low-passed at 3 Hz.
- [x] Per-seat per-stroke gating on the longest gap without fresh samples (> 0.30 s = degraded),
      replacing the held-run fraction. Holes of 0.5-1 s in the drive biased same-oar offsets by
      +270 ms on 3 Oct.
- Checked with scripts/engine_check.py: 10 Oct merged pieces 346/350 and 290/307 strokes OK,
  medians -2 / -90 ms (bow ahead); 3 Oct same-oar pair -2 ms, IQR 20 ms on clean strokes.
