# RESEARCH — Sensor Data Normalization & Synchronicity Extraction

Status: design reference for the ingest-time analysis pipeline. Describes the
hardware, the data we actually receive today, the signal-processing method for
turning arbitrarily-mounted IMU data into a rigorous cross-rower synchronicity
metric, and the literature basis for each choice. Written to be actionable and
auditable — every non-obvious method cites prior art.

Scope note: **assume the raw data we have today (fields, rates, timestamps) is
fixed.** All algorithm choices in §3–§7 must work with that. Improvements that
require changing the sensor config or the phone app are collected in §10 (Further
Work — Sensor), deliberately separated so the pipeline does not depend on them.

---

## 1. The hardware — WitMotion WT9011DCL

A 9-axis Bluetooth AHRS IMU (MPU-9250-class core: 3-axis accel + 3-axis gyro +
3-axis magnetometer + on-chip Kalman fusion). [Datasheet][ds].

| Spec | Value |
|---|---|
| Accelerometer | ±16 g, 16-bit, ~0.01 g accuracy |
| Gyroscope | ±2000 °/s, 16-bit, ~0.05 °/s stability |
| Magnetometer | 3-axis (on chip) |
| Fusion output | 3-axis Euler angle **and quaternion** (dynamic Kalman filter) |
| Output rate | 0.2 Hz – **200 Hz**, factory default 10 Hz |
| Link | BLE 5.0 (nRF52832), ≤90 m |
| Protocol | WIT: framed packets `0x55 <type> <payload…>` |

The chip can output all nine raw axes plus quaternion plus a time register.
**What it *can* do and what our app *streams* are different things — see §2.**

### WIT packet types (relevant subset)
| Frame | Meaning |
|---|---|
| `0x55 0x61` | **Combined**: accel(3) + angular-velocity(3) + Euler-angle(3), 20 bytes. *This is our data.* |
| `0x55 0x50` | **TIME**: on-chip clock (Y/M/D/H/M/S/ms), 10 bytes |
| `0x55 0x54` | Magnetometer (not in the 0x61 combined frame) |
| `0x55 0x59` | Quaternion (not in the 0x61 combined frame) |

Config is via `0xFF 0xAA <reg> <lo> <hi>` frames: `RRATE`(0x03) sets rate,
`RSW`(0x02) selects which streams are emitted.

---

## 2. What we actually receive today (the data contract)

Verified against `SyncRow/…/BleDeviceClient.kt` and the live InfluxDB.

- **Fields per sample:** `ax, ay, az` (g), `wx, wy, wz` (°/s angular velocity),
  `roll, pitch, yaw` (°, fused Euler). **No magnetometer, no quaternion, no
  sensor timestamp** reach InfluxDB — only the 0x61 combined frame is parsed.
- **Configured rate:** `RRATE_50HZ` for on-water multi-sensor use
  (`BleDeviceClient.targetRateRegister`); the code notes 100 Hz is used only for
  bench / 1–2-sensor tests. **Effective rate is BLE-bandwidth-limited** and drops
  under multiple simultaneous sensors on a saturated link (documented in-code as
  the reason the rate is held at 50 Hz). So *treat per-sensor effective rate as
  variable and estimate it per interval* — do not assume 50 Hz.
- **Timestamp = phone BLE-arrival time.** Each sample's `_time` in InfluxDB is
  `System.currentTimeMillis()` when the phone received the BLE notification, not
  when the sensor sampled. This is the crux of the timing-noise problem (§4, §6).
- **The sensor DOES emit a timestamp, and we currently throw it away.** The app
  can enable the `0x50` TIME stream (`enableTimePacket` / `RSW_TIME_BIT`) but the
  handler explicitly **only counts arrivals and returns without decoding** — and
  enabling it competes for BLE bandwidth with the data stream. Decoding and using
  it is the single highest-value app-side change (§10).

**Consequence for this pipeline:** we work with phone-arrival timestamps at a
variable ~10–50 Hz effective rate, and we lean hard on interpolation and
per-interval rate estimation. Everything below is designed around that reality.

---

## 3. Channel normalization — the synthetic axis

### 3.1 Why the current "dominant Euler channel" approach is wrong
Today's code (both `analyze_async.py` offline and `StrokeAnalyzer.kt` on-phone)
**selects one** of `pitch/roll/yaw` — the one with the largest swing (IQR /
running variance) — and discards the other two. This is unsound for three
reasons:

1. **The sensor frame is arbitrary.** The IMU is taped to the oar at an unknown
   rotation. "yaw" is not boat heading — it is rotation about whichever sensor
   axis happens to point wherever the tape put it. The Euler triple is
   world-referenced through the fusion, but which *world* axis the stroke lands
   on is mounting luck. Selection therefore couples the metric to how the sensor
   was stuck on.
2. **Euler angles are the wrong coordinate system.** They are fusion outputs
   (gyro integrated, corrected by accel + mag), subject to drift, ±180° wrap, and
   gimbal effects near singularities. The **raw gyro** is the cleanest motion
   signal the hardware produces and is not used for detection at all today (only
   for a phase check).
3. **The oar has two distinct rotations the picker can't separate.** The *sweep*
   (the stroke, ~0.2–0.75 Hz) and the *feather/square roll* about the shaft
   (±90°, twice per stroke, higher frequency). Depending on mounting either may
   "win," so sensor A can lock onto sweep while sensor B locks onto feather —
   and then their catch times mark physically different instants: a built-in
   per-sensor bias in exactly the quantity we measure across sensors.

### 3.2 The fix: construct a per-sensor physical frame from the data
Do not *select* an axis — *build* one. This is the published **functional
sensor-to-segment calibration** method: exploit angular velocity to define the
functional rotation axis, using PCA on the gyro signal. It is standard for
estimating joint axes (knee, elbow) from IMUs regardless of sensor placement
([Sensors 2018, PCA knee-axis][pca-knee]; [Sensors 2020 S2S review][s2s]).

Per sensor, per interval:

1. **Gravity → "down" axis.** Low-pass `(ax, ay, az)` (accel = gravity + linear
   acceleration, inseparable instantaneously; the low-frequency component is
   gravity). Normalize → unit vector `d` = true down in the sensor frame. This is
   the one absolute reference we can always recover with no calibration.
2. **Sweep axis via gyro PCA.** Compute the 3×3 covariance of the raw gyro
   vectors `(wx, wy, wz)` over the interval; its **first principal component** is
   the direction carrying the most rotational energy — the physical sweep axis,
   as a *weighted blend of all three gyro channels* (e.g. `0.61·wx − 0.33·wy +
   0.72·wz`), not a selection. All samples from all channels contribute; this is
   simultaneously the optimal 1-D projection and a noise filter, because the
   rigid oar's sweep truly lives along one fixed axis in the sensor frame.
3. **Feather rejection.** PC1 is not *guaranteed* to be the sweep — a heavy
   feather could dominate. Take the candidate PCs and check the **dominant
   spectral peak** of each projected signal; keep the one whose peak sits in the
   **stroke band (~0.2–0.75 Hz = 12–45 spm)** rather than the feather signature.
   (Spectral step requires uniform resampling first — see §6.1.)
4. **Orthonormal frame.** Orthogonalize the sweep axis against `d` (Gram-Schmidt),
   `third = d × sweep`. Now `{down, sweep, third}` is a complete physical frame
   built purely from data — mounting normalization *by construction*.
5. **Project.** Project the gyro (and, if useful, accel) onto this frame →
   a **signed 1-D sweep signal** (sign preserved, so drive vs recovery are
   distinguishable — unlike `‖ω‖` magnitude, which rectifies and destroys phase).
6. **Amplitude normalization.** Z-score the projected signal per sensor. Removes
   the residual geometry effect (sensors at different radii/angles have different
   amplitude) so cross-sensor comparison is clean.

### 3.3 PCA sign ambiguity (must be handled deterministically)
An eigenvector `w` and `−w` are equivalent, so PCA's axis direction flips
arbitrarily run-to-run and sensor-to-sensor. This does not matter for spectra but
**does** matter for time-domain and cross-sensor sign ([Sandia sign-ambiguity
report][sign]). Resolve in two stages:

1. **Deterministic convention** on the eigenvector (e.g. force the
   largest-magnitude loading positive) so a given sensor's axis is reproducible.
2. **Physical sign** via cross-correlation of each sensor's projected sweep
   signal against the reference sensor — align them to the same drive/recovery
   polarity. (This cross-correlation is *also* our second lag estimator, §6.3 —
   free.)

### 3.4 What we deliberately keep
- **PC2 ≈ the feather roll** — a real, useful signal (squaring/feathering timing
  is a coached metric). Store it as its own derived series later.
- **Invariant scalars** `‖ω‖`, `‖a‖`, `ω·a` — orientation-proof; good for
  validation and gross phase, unsigned so not for fine timing.
- **The Euler-IQR picker as a fallback** for any sensor whose gyro is missing or
  too weak for a stable PCA (the code already has the mirror fallback).

---

## 4. The timing chain (why timing is the hard part)

Cross-rower synchronicity **is a timing measurement**, so the timing chain sets
the noise floor:

```
sensor samples (fixed internal rate, accurate intervals)
   → BLE transmit (queuing, retransmit, connection-interval quantization)      ← JITTER injected here
   → phone receives notification, stamps System.currentTimeMillis()            ← this is our _time
   → batched, uploaded to InfluxDB
```

The sensor's *sampling* is regular; the **arrival** is not. BLE connection
intervals, multi-sensor contention, and OS scheduling put jitter — plausibly
**tens of ms** — between when a sample was taken and when it was stamped. That
jitter is *per-sensor and uncorrelated*, so it lands directly in the cross-sensor
offset we report. **No server-side math removes it**; we can only (a) reduce its
effect with resampling/averaging, (b) quantify it as an error bar, and (c) fix it
at the source later (§10, the 0x50 timestamp).

Implication: an async number of, say, 8 ms may be **below the resolution of the
measurement chain.** Every reported offset must ship with an uncertainty (§6.4).

---

## 5. Detection algorithm

### 5.1 Catch detection — median crossing (keep, with one fix)
Detect each catch as the **interpolated crossing of the projected sweep signal
through its median.** Rationale (sound, and in the code today): the signal moves
*fastest* through its midpoint, so timing is least sensitive to amplitude noise
there — far better than peak/trough detection, which lingers at extrema.

**Fix vs current offline code:** `analyze_async.py` uses a **global** median over
the whole active window; baseline drift (boat turn, sensor settle) then shifts or
drops crossings. Use a **running median** (or detrend first) — the phone's Kotlin
version already does this and is correct here. Sub-sample crossing time by linear
interpolation between the two straddling samples (Eq. as in patent disclosure
M3).

### 5.2 Second detector — wavelet (candidate cross-check)
A 2024 study determines drive-start/drive-end across stroke types via an
**undecimated wavelet transform** of a hull IMU ([Sensors 2024][wavelet]).
Recommended as an independent second detector; agreement between median-crossing
and wavelet is a per-stroke confidence signal.

---

## 6. Resolution & timing precision (best-practice preprocessing)

These are the disciplines the literature says we are currently missing.

### 6.1 Resample to a uniform grid *first*
Phone-arrival timestamps are non-uniform (jittered, occasional gaps). Feeding
that to any FFT / spectral / cross-correlation step gives **wrong frequencies** —
those algorithms assume uniform spacing ([Kraken, irregular sampling][kraken]).
Standard fix: **linearly interpolate each sensor onto a synthesized uniform
time-base** at a common target rate before spectral work (the stroke-band check
§3.2, and cross-correlation §6.3). This is also where "highest possible frequency,
including interpolated" is realized: upsample all sensors to a common high-rate
grid so their samples are directly comparable. (Median-crossing detection §5.1
can run on the native samples; the *spectral and correlation* steps need the
uniform grid.)

### 6.2 Sub-sample catch timing
Linear interpolation of the median crossing (§5.1) already gives sub-sample
timing — essential at 10–50 Hz where a stroke is only 20–100 samples.

### 6.3 Cross-correlation lag as an independent estimator
Once every sensor is projected onto its sweep axis (§3) and resampled onto the
common grid (§6.1), **cross-correlate sensor pairs per stroke (or per window)**
to estimate lag directly — a second, method-independent estimate of async that
does not depend on catch detection at all. Classic sub-sample time-delay
estimation ([ScienceDirect TDE][tde]). **Use Gaussian peak interpolation, not the
3-point parabola** — parabolic fitting has a documented bias at low
sample-rate-to-signal-frequency ratio (exactly our 10–50 Hz regime), and Gaussian
is the more robust approximation of the correlation peak ([IEEE interpolation
methods][interp]). Two estimators agreeing (catch-based + correlation-based) is
strong evidence — and strengthens the IP.

### 6.4 Timing uncertainty (ship an error bar)
At each crossing, estimate timing uncertainty from the local signal slope and
noise (steep, clean crossing → tight; shallow, noisy → loose). Aggregate to a
per-sensor, per-interval timing-uncertainty figure. **Every async number is
reported with this uncertainty**, so "0–20 ms" buckets are honestly labeled as
possibly-below-resolution (§4).

---

## 7. Cross-sensor synchronicity (the output metric)

Replace the current thin metric with a richer, honest one:

- **Reference = the stroke seat (highest seat index).** Rowing convention: the
  crew follows stroke, so offsets are anchored on stroke — not on an arbitrary
  "most-crossings" sensor (current offline behavior), which could itself be the
  out-of-time rower. `stroke_offset ≡ 0` by definition. (The phone's
  `StrokeAnalyzer` currently anchors on the *lowest* seat index — inverted; see
  `docs/FIXES.md`.) A crew-centroid reference is also computed for the symmetric
  spread summary, but stroke is the anchor for the coaching view.
- **Per-seat signed offset vs stroke (ms):** who is early/late, not just the
  peak-to-peak spread. Same semantics as the phone's live lateness — app and
  portal agree. (Current offline code stores only `max−min`, hiding who.)
- **Spread / dispersion** across the crew as a summary.
- **Two independent estimators** per stroke: catch-based (§5) and
  correlation-based (§6.3); report both + their agreement.
- **Allow 2 sensors.** Current `MIN_SENSORS_FOR_ASYNC=3` kills a double, where
  pairwise offset is perfectly meaningful. Pipeline: N≥2.
- **Keep every stroke; flag, don't drop.** Drill filtering and hardcoded SPM
  bounds (max 36 — sprints exceed it) were tuned for the *research study*; in the
  product they silently delete data. Emit `quality` flags (`drill`,
  `out_of_range`, `low_confidence`) and let the UI filter; the data survives.
- **The continuous "spread score"** (`1/(1+spread)`) may stay as a *secondary,
  clearly-labeled* display series — but recomputed at **full raw resolution**,
  never from the 200 ms display-aggregated data (the regression we are fixing).

---

## 8. Two tiers: real-time (phone) vs authoritative (portal)

The **same algorithm** (§3–§7) runs in two tiers, but they are **separate
implementations with different roles** — not shared code (phone is Kotlin, portal
is Python). We accept that split deliberately:

- **Portal = authoritative, system of record.** Full raw resolution, whole
  interval available, second estimator (§6.3), timing uncertainty (§6.4). Nothing
  the phone computes is ingested; the portal **recomputes from raw** independently.
- **Phone = real-time, display-only, disposable.** Expected to be *generally*
  accurate for live feedback, never authoritative. Because its output is never
  stored or trusted downstream, it is free to be pragmatic and to **revise
  itself** (backfill, §8.3).

### 8.1 Causal windowing (whole-interval → trailing window)
The portal computes the synthetic axis (§3.2) and median (§5.1) over the whole
interval. The phone only has data up to *now*, so it runs the identical pipeline
over a **trailing window** (target ~30 s). "Whole interval" is just an infinite
window — make the engine windowed and both tiers share one conceptual path. The
PCA axis stabilizes within a handful of strokes; it is the *timing* that is noisy
early, surfaced honestly as wide uncertainty (§6.4), not hidden.

### 8.2 Cold start + adaptive quality gate (warm-up rejection)
The window must work from **stroke 1** and must **exclude non-rowing noise**
(pre-piece maneuvering, spinning, backing — async through the roof, no periodic
structure). So the window is **expanding-then-sliding** and **quality-gated**:

- From stroke 1, compute on whatever history exists (2–3 strokes); confidence
  grows as the window fills to ~30 s, then it slides.
- A stroke is **admitted to the window only if it looks like rowing**: rotational
  energy above a floor **and** a clean in-band period (stroke-band spectral peak /
  low period-variance). Warm-up chaos fails the in-band test and is excluded from
  the estimate window — but still shown, flagged, so the rower sees "not rowing
  yet." This is the causal form of the portal's active-window detection (§ M7c of
  the offline code).

### 8.3 Backfill / repaint (display only)
Early strokes render from a bad/empty window and look like trash. By ~stroke 3 the
window holds real rowing and the axis/median have locked in. The phone then
**re-runs the last few strokes against the now-good model and repaints them** —
the live strip *heals backward* as confidence arrives (which also reads as the
crew "locking in"). Legitimate precisely because it is display, not a record; cap
the retro horizon to the trailing buffer, never the whole piece.

### 8.4 Shared validity rule (the one thing that must NOT drift)
The two tiers may have separate code, but they **must share one definition of
"valid rowing stroke"** — the §8.2 gate criteria (in-band periodicity + energy
floor + period-variance). If the phone proudly displays a stroke the portal
discards, trust breaks. Pull this out as a **named spec** (`stroke_validity`) that
both implementations cite; it is the only place where app/portal divergence would
actually corrupt the product. Everything else can differ (windowing, backfill,
second estimator) because only the portal is authoritative.

### 8.5 Per-seat, per-stroke quality gating (shared spec, both tiers)
Quality is judged **per seat, per stroke** — never per boat. A crew of eight has
eight independent sensors on eight independent people; a single sensor spacing out
(BLE starvation → zero-order-held samples, §6/§10; see the held-sample finding) or
a single rower catching a crab must degrade **only that seat's affected strokes**,
leaving every other seat and every other stroke live. Two per-seat states:

- `degraded_signal` — **acquisition** failure: the sensor's window is mostly
  held/duplicated samples (`SensorFrame.held`, longest hold-run > `max_held_run_frac`
  of the stroke). Unmeasurable regardless of rowing.
- `low_confidence` — **match** failure: fresh data, but the seat's waveform doesn't
  cross-correlate with the reference (peak ρ < `min_corr`).

Order matters: check acquisition **before** match, so a held seat reads as "the
sensor spaced out," not "the rower rowed badly." A held **reference** is the one
whole-stroke void (`reference_degraded` / `reference_bad` seats) — everything is
measured relative to stroke, so a bad reference can't be rescued per-seat.
Aggregates (median offset, crew spread) consume each seat's `ok` strokes
independently.

**Portal vs phone — what actually transfers (do not overstate this as a "mirror").**
The portal is authoritative and implements the full spec in `crosssensor.seat_status`.
The phone real-time tier implements a **subset/approximation**, and the two can and
do differ:
- `degraded_signal` (acquisition) transfers in spirit but not identically: portal =
  longest hold-run inside a stroke window; phone = time since the last fresh BLE
  sample > `HELD_RUN_FRAC` of a stroke. Both flag "the sensor spaced out."
- `low_confidence` (match quality) has **no phone equivalent** — the phone has no
  cross-correlation, so a fresh seat rowing a genuinely different rhythm reads OK on
  the phone while the portal would discard it. This is a real §8.4 gap: the phone can
  display a stroke the portal drops. Accepted because only the portal is authoritative;
  the phone value is a live estimate, not a verdict.
- The stroke-band (`STROKE_BAND_HZ`) is **not shared in code** — the phone selects a
  channel by variance and never applies the band. "Shared spec" here means shared
  *intent*, enforced only in the portal.
The single hard invariant both tiers DO honour: quality is per-seat, so one bad seat
never blanks the crew.

---

## 9. Best-practices checklist (audit)

| Practice | Source | Status in current code | Pipeline |
|---|---|---|---|
| Functional axis via gyro-PCA (not channel selection) | [pca-knee][pca-knee], [s2s][s2s] | ✗ selects one Euler channel | ✓ synthetic axis |
| Gravity-anchored sensor frame | standard | ✗ | ✓ |
| Gyro (not Euler) for oar rotation | [ETH thesis][eth], [sensor network][net] | partial (Euler used) | ✓ raw gyro |
| Uniform resample before spectral/correlation | [kraken][kraken] | ✗ | ✓ |
| Sub-sample catch timing | patent M3 | ✓ | ✓ (keep) |
| Running (not global) median | Kotlin RT | ✗ offline | ✓ |
| Gaussian (not parabolic) peak interp | [interp][interp] | n/a | ✓ |
| Deterministic PCA sign convention | [sign][sign] | n/a | ✓ |
| Report timing uncertainty | §4 | ✗ | ✓ |
| Second detector / estimator (wavelet, x-corr) | [wavelet][wavelet], [tde][tde] | ✗ | ✓ |
| Keep-and-flag (don't drop strokes) | product | ✗ drops | ✓ |
| Per-seat quality gating (one bad seat ≠ dead boat) | §8.5, product | ✗ per-stroke/all-seats | ✓ per-seat status (both tiers) |
| Held/duplicated-sample detection (`degraded_signal`) | held-sample finding | ✗ | ✓ (both tiers) |
| Allow N≥2 sensors | product | ✗ (needs 3) | ✓ |
| Reference = stroke seat (highest index) | rowing convention | ✗ most-crossings; phone uses lowest | ✓ stroke |
| Quality-gated window / warm-up rejection | §8.2 | partial (offline active-window) | ✓ both tiers |

---

## 10. Further work — the sensor (LATER; requires app / firmware changes)

Deliberately out of scope for the current pipeline, but these are the biggest
real-world accuracy levers, roughly in order of value:

1. **Decode and use the 0x50 TIME packet — de-jitter against the sensor clock.**
   The sensor emits a millisecond on-chip timestamp; the app can enable it but
   currently only counts arrivals. If the app decoded it and paired it with each
   0x61 sample, we'd have the sensor's own monotonic clock — whose *inter-sample
   intervals are accurate* even if the absolute RTC is unset. We then anchor that
   clock to the phone clock over the session (robust linear fit of sensor-time vs
   phone-time) to recover absolute time **without BLE arrival jitter.** This
   directly attacks the §4 noise floor — likely the single biggest win. Caveat:
   the TIME stream competes for BLE bandwidth (in-code concern); measure the
   throughput cost, and/or raise the connection throughput to compensate.
2. **Raise the output rate off 50 Hz.** Native is 200 Hz. Every doubling roughly
   halves sub-sample interpolation error. Gated by BLE bandwidth with multiple
   sensors — needs a link-budget / connection-interval study, possibly fewer
   sensors per link or 2.4 GHz RF variant.
3. **Set the sensor RTC at connect** so the 0x50 timestamp is absolute, not just
   monotonic — simplifies the anchoring in (1).
4. **Stream raw magnetometer + quaternion** (0x54 / 0x59, or a wider RSW mask).
   Quaternion > Euler (no gimbal lock / wrap) if we ever want fused orientation.
   Raw mag would give an absolute heading reference — but only with per-sensor
   **hard/soft-iron calibration**, which the rigger/oarlock metal makes hard and
   which we have explicitly deferred. Not needed for sync (relative timing needs
   consistent frames, not north).
5. **On-device timestamping of the sample, not arrival** — if any firmware path
   allows the sensor to tag samples before transmit, prefer it over (1).

---

## 11. Known limitations / honest caveats

- **BLE-arrival timestamps set a timing noise floor** (§4) that no server math
  removes. Until §10.1, sub-20 ms async values are near/at the resolution limit;
  always shown with uncertainty.
- **Effective rate is variable** and can fall well below 50 Hz under multi-sensor
  BLE load — estimate per interval, never assume.
- **The sync→speed causal premise is contested** in the literature (antiphase
  crews can be *more* efficient — [Greidanus velocity-fluctuations][grei];
  ["Don't Rock the Boat"][boat]). The measurement (timing offset) is solid; any
  *speed-loss* claim stays a computed estimate, per the patent disclosure.
- **No iron calibration** ⇒ magnetometer heading is unavailable/unreliable; we do
  not use it.

---

## 12. References

- [ds]: WitMotion WT9011DCL datasheet — https://www.sensor-test.de/assets/Fairs/2025/ProductNews/PDFs/WT9011DCL-Datasheet.pdf
- [pca-knee]: Auto-calibrating knee flexion-extension axis via PCA on IMU angular velocity, *Sensors* 2018 — https://doi.org/10.3390/s18061882
- [s2s]: Sensor-to-Segment Calibration Methodologies (systematic review), *Sensors* 2020 — https://www.mdpi.com/1424-8220/20/11/3322/htm
- [net]: An IMU-based sensor network to monitor rowing technique on the water — https://www.researchgate.net/publication/235834654
- [eth]: Rowing Performance Analysis Using Motion Sensors (ETH doctoral thesis) — https://www.research-collection.ethz.ch/server/api/core/bitstreams/3fdce966-42fb-4880-80f6-30df8513895a/content
- [wavelet]: On-water rowing stroke kinematics via undecimated wavelet transform of a hull accelerometer, *Sensors* 2024 — https://doi.org/10.3390/s24186085
- [kleshnev]: Using IMU sensors to compare rowing ergometers with rowing on the water (Knarr, Kwoun, Kleshnev) 2024 — https://journals.sagepub.com/doi/10.1177/17543371241256165
- [tde]: On the application of the cross-correlation function to sub-sample time-delay estimation, *Signal Processing* — https://www.sciencedirect.com/science/article/abs/pii/S1051200406001230
- [interp]: Interpolation methods for time-delay estimation using cross-correlation (parabolic vs Gaussian bias) — https://pubmed.ncbi.nlm.nih.gov/18238424/
- [kraken]: Spectral analysis of irregularly sampled signals — https://krakensystems.co/blog/2019/irregularly-sampled-signal
- [sign]: Bro, Acar, Kolda — Resolving the sign ambiguity in the SVD (Sandia) — https://www.osti.gov/servlets/purl/920802
- [grei]: Greidanus et al., Drag and Power-loss in Rowing Due to Velocity Fluctuations, *Procedia Engineering* 2016 — https://www.sciencedirect.com/science/article/pii/S1877705816307469
- [boat]: Don't Rock the Boat: How Antiphase Crew Coordination Affects Rowing, *PLOS ONE* — https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3559869/

[ds]: https://www.sensor-test.de/assets/Fairs/2025/ProductNews/PDFs/WT9011DCL-Datasheet.pdf
[pca-knee]: https://doi.org/10.3390/s18061882
[s2s]: https://www.mdpi.com/1424-8220/20/11/3322/htm
[net]: https://www.researchgate.net/publication/235834654
[eth]: https://www.research-collection.ethz.ch/server/api/core/bitstreams/3fdce966-42fb-4880-80f6-30df8513895a/content
[wavelet]: https://doi.org/10.3390/s24186085
[tde]: https://www.sciencedirect.com/science/article/abs/pii/S1051200406001230
[interp]: https://pubmed.ncbi.nlm.nih.gov/18238424/
[kraken]: https://krakensystems.co/blog/2019/irregularly-sampled-signal
[sign]: https://www.osti.gov/servlets/purl/920802
[grei]: https://www.sciencedirect.com/science/article/pii/S1877705816307469
[boat]: https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3559869/
