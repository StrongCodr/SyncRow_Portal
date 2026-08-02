# Fix / Build List

Canonical checklist for the ingest-time analysis pipeline. Design rationale lives
in [`../RESEARCH.md`](../RESEARCH.md). Phone items are in the **SyncRow** (Android)
repo; everything else here.

## Portal analysis engine — `srow/analysis/` (single source of truth)
- [ ] Extract/replace `analyze_async` logic into `srow/analysis/`; CLI + worker + backfill import it.
- [ ] Synthetic axis: gravity `down` + gyro-PCA `sweep` + cross; feather rejection (stroke-band); deterministic PCA sign; project to signed 1-D; z-score.
- [ ] Compute on RAW; estimate per-sensor rate; uniform resample before spectral/correlation.
- [ ] Running-median crossing + sub-sample catch timing.
- [ ] Second estimator: cross-correlation lag, Gaussian peak interp.
- [ ] Per-crossing timing uncertainty (error bars).
- [ ] Reference = stroke seat (highest index); signed per-seat offset + crew-centroid spread.
- [ ] Allow N≥2 sensors; keep-and-flag strokes (`drill`/`out_of_range`/`low_confidence`).
- [ ] Shared `stroke_validity` spec (phone + portal cite it).
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
- [ ] Confirm `seat` tag ordering unambiguous.

## Sensor / firmware — LATER (bench + app/firmware)
- [ ] Bench: RSW bit-sweep; is `0x61` fixed vs composed; `0x50` throughput cost; rate vs sensor-count; connection interval; decode a `0x50`.
- [ ] Decode `0x50` timestamp → de-jitter; per-packet sequence counter (drop detection).
- [ ] Raise rate off 50 Hz; 9-sensor plan (RF cascade / onboard logging); set RTC; mag/quaternion only if needed.
