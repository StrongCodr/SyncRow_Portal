"""SyncRow analysis engine — single source of truth for synchronicity.

Turns arbitrarily-mounted per-sensor IMU data into per-stroke cross-rower timing.
Used by the offline CLI, the ingest worker, and backfill. Design: ../RESEARCH.md.

Pipeline (per sensor): estimate rate -> synthetic physical frame (gravity +
gyro-PCA sweep) -> project to signed 1-D sweep signal -> running-median catch
detection with sub-sample timing. Then cross-sensor: reference = stroke seat,
signed per-seat offsets + cross-correlation second estimator.
"""

from .frame import SensorFrame, build_sensor_frame
from .resample import estimate_rate, to_uniform
from .detect import Catch, detect_catches
from . import validity

__all__ = [
    "SensorFrame",
    "build_sensor_frame",
    "estimate_rate",
    "to_uniform",
    "Catch",
    "detect_catches",
    "validity",
]
