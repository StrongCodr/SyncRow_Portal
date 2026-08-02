# Fix List

Cross-cutting things to fix, tracked here so they don't get lost. Phone items
live in the **SyncRow** (Android) repo; portal items here.

## SyncRow app (phone)

- [ ] **Reference seat is inverted.** `StrokeAnalyzer.kt` picks the reference
  (stroke) rower as the **lowest** seat index. Stroke seat is by convention the
  **highest** indexed seat. Change the reference selection to highest seat index
  so the phone's live lateness anchors on stroke — and so app and portal agree.
  (`addSensor` / `referenceMac` selection.)
- [ ] Confirm the `seat` tag values reliably encode seat order (Seat N vs Cox,
  sweep vs sculling numbering) so "highest = stroke" is unambiguous. Reconcile
  with the portal analysis engine's reference resolution.

## Portal analysis (the `srow/analysis/` rewrite)

- [ ] Reference = stroke seat (highest index); offsets = `catch_seat − catch_stroke`.
- [ ] See RESEARCH.md §3–§7 for the full algorithm change-list (synthetic
  gyro-PCA axis, raw-resolution compute, running median, cross-correlation second
  estimator, keep-and-flag, N≥2, timing uncertainty).
