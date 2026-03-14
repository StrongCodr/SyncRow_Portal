# SyncRow Data Model

This document describes the data schemas used in SyncRow.

## InfluxDB Structure

### Database Organization

- **URL**: InfluxDB Cloud or self-hosted instance
- **Organization**: Your InfluxDB organization
- **Bucket**: `syncrow` (configurable)

### Measurements

SyncRow works with two measurements:

1. **imu** - Inertial Measurement Unit data from sensors
2. **phone_location** - GPS data from phones

## IMU Measurement

Data from accelerometers and gyroscopes mounted on oars or seats.

### Tags (Indexed, for filtering)

| Tag | Description | Example |
|-----|-------------|---------|
| `intervalId` | Unique interval identifier | `1705329000000` |
| `interval_id` | Alternative interval ID | `session_2024_01` |
| `intervalName` | Human-readable name | `Morning Practice` |
| `sensorId` | Sensor hardware ID | `IMU-001` |
| `sensor_id` | Alternative sensor ID | `oar_bow` |
| `seat` | Seat/position label | `BOW`, `STROKE`, `2` |

### Fields (Numeric values)

| Field | Description | Units | Range |
|-------|-------------|-------|-------|
| `ax` | X-axis acceleration | m/s² | -16 to +16 |
| `ay` | Y-axis acceleration | m/s² | -16 to +16 |
| `az` | Z-axis acceleration | m/s² | -16 to +16 |
| `roll` | Roll angle | degrees | -180 to +180 |
| `pitch` | Pitch angle | degrees | -180 to +180 |
| `yaw` | Yaw/heading angle | degrees | -180 to +180 |

### Typical Characteristics

- **Sampling Rate**: 50-200 Hz (typically 100 Hz)
- **Duration**: 1-60 minutes per interval
- **Data Volume**: ~360,000 rows per sensor per hour at 100 Hz

### Example Data

```
time                    sensorId  seat    ax      ay      az      roll    pitch   yaw
2024-01-15T14:30:00.000 IMU-001   BOW     0.15    -0.22   9.81    5.2     -2.1    45.3
2024-01-15T14:30:00.010 IMU-001   BOW     0.18    -0.19   9.79    5.4     -2.0    45.5
2024-01-15T14:30:00.020 IMU-001   BOW     0.21    -0.15   9.82    5.7     -1.8    45.8
```

## Phone Location Measurement

GPS data from phones used for positioning and track recording.

### Tags

| Tag | Description | Example |
|-----|-------------|---------|
| `intervalId` | Interval identifier | `1705329000000` |
| `deviceId` | Phone/device ID | `phone_coach` |
| `device_id` | Alternative device ID | `pixel_7` |

### Fields

| Field | Description | Units | Range |
|-------|-------------|-------|-------|
| `latitude` | GPS latitude | degrees | -90 to +90 |
| `longitude` | GPS longitude | degrees | -180 to +180 |
| `altitude` | Elevation above sea level | meters | varies |
| `speed` | GPS-reported speed | m/s | 0 to 50+ |
| `accuracy` | Horizontal accuracy | meters | 1 to 100+ |
| `bearing` | Direction of travel | degrees | 0 to 360 |

### Typical Characteristics

- **Sampling Rate**: 1-10 Hz (typically 1 Hz)
- **Duration**: Same as IMU intervals
- **Data Volume**: ~3,600 rows per device per hour at 1 Hz

### Example Data

```
time                    deviceId      latitude    longitude   altitude  speed  accuracy  bearing
2024-01-15T14:30:00.000 phone_coach   42.360100   -71.058900  10.5      2.5    5.0       45.0
2024-01-15T14:30:01.000 phone_coach   42.360105   -71.058895  10.6      2.6    4.8       46.2
2024-01-15T14:30:02.000 phone_coach   42.360111   -71.058889  10.5      2.7    5.1       45.8
```

## Data Relationships

### Interval Correlation

IMU and phone_location data are linked by the `intervalId` tag:

```
IMU Data (100 Hz)                    Phone Location (1 Hz)
├── intervalId: 1705329000000        ├── intervalId: 1705329000000
├── sensorId: IMU-001                ├── deviceId: phone_coach
├── time: 14:30:00.000               ├── time: 14:30:00.000
│   ax, ay, az, roll, pitch, yaw     │   lat, lon, alt, speed
├── time: 14:30:00.010               │
│   ...                              │
...                                  ├── time: 14:30:01.000
├── time: 14:30:01.000               │   lat, lon, alt, speed
│   ...                              ...
```

### Multi-Sensor Intervals

A single interval may contain data from multiple sensors:

```
Interval: 1705329000000
├── Sensor: IMU-001 (BOW)      [100 Hz]
├── Sensor: IMU-002 (2)        [100 Hz]
├── Sensor: IMU-003 (3)        [100 Hz]
├── Sensor: IMU-004 (STROKE)   [100 Hz]
└── Phone: phone_coach         [1 Hz]
```

## Flux Query Examples

### List Available Intervals

```flux
import "influxdata/influxdb/schema"
schema.tagValues(bucket: "syncrow", tag: "intervalId")
```

### Load IMU Data for Interval

```flux
from(bucket: "syncrow")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "imu")
  |> filter(fn: (r) => r.intervalId == "1705329000000")
  |> sort(columns: ["_time"])
```

### Load Aggregated Data (Downsampled)

```flux
from(bucket: "syncrow")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "imu")
  |> filter(fn: (r) => r.intervalId == "1705329000000")
  |> aggregateWindow(every: 100ms, fn: mean, createEmpty: false)
  |> sort(columns: ["_time"])
```

### Get Time Bounds

```flux
from(bucket: "syncrow")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "imu")
  |> filter(fn: (r) => r.intervalId == "1705329000000")
  |> keep(columns: ["_time"])
  |> first()
```

### Load GPS Track

```flux
from(bucket: "syncrow")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "phone_location")
  |> filter(fn: (r) => r.intervalId == "1705329000000")
  |> sort(columns: ["_time"])
```

## DataFrame Structures

### IMU DataFrame (Wide Format)

After pivoting from InfluxDB long format:

```python
DataFrame columns: ['time', 'source', 'ax', 'ay', 'az', 'roll', 'pitch', 'yaw']
```

Where `source` is constructed as `"{sensorId} ({seat})"` or just `"{sensorId}"`.

### Location GeoDataFrame

```python
GeoDataFrame columns: ['time', 'device_id', 'latitude', 'longitude',
                       'altitude', 'speed', 'accuracy', 'bearing', 'geometry']
```

The `geometry` column contains Shapely `Point` objects for mapping.

## Data Processing

### Angle Unwrapping

Roll, pitch, and yaw values wrap at ±180°. The `unwrap_angles()` function corrects this:

```
Before: [170, 175, -175, -170]  (discontinuity at 180°)
After:  [170, 175, 185, 190]    (continuous)
```

### Source Label Construction

The display label for each sensor is constructed from available tags:

```python
if sensor and seat:
    return f"{sensor} ({seat})"  # "IMU-001 (BOW)"
elif sensor:
    return sensor                 # "IMU-001"
elif seat:
    return seat                   # "BOW"
else:
    return "UNKNOWN"
```
