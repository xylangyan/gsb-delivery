# Stubs for pysolar.backfill (Python 3.6)

import datetime
from typing import List, Sequence, Tuple, Any

class StationProfile:
    effective_from: datetime.datetime
    latitude_deg: float
    longitude_deg: float
    elevation: float
    temperature: float
    pressure: float
    azimuths: List[float]
    altitudes: List[float]
    label: Any
    max_horizon_slope: float
    max_horizon_height: float
    min_horizon_height: float
    refraction_jump: float
    def __init__(self, effective_from: datetime.datetime,
                 latitude_deg: float, longitude_deg: float,
                 elevation: float, temperature: float, pressure: float,
                 horizon_points: Sequence[Tuple[float, float]],
                 label: Any = ...) -> None: ...
    def horizon_height(self, azimuth: float) -> float: ...
    def clearance(self, when: datetime.datetime) -> float: ...
    def admitted(self, when: datetime.datetime) -> bool: ...

class ChangeSegment:
    action: str
    start_index: int
    end_index: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    profile_versions: List[Any]
    effective_points: List[datetime.datetime]
    def __init__(self, action: str, start_index: int, end_index: int,
                 start_time: datetime.datetime, end_time: datetime.datetime,
                 profile_versions: List[Any],
                 effective_points: List[datetime.datetime]) -> None: ...

def reevaluate_admission(
    start: datetime.datetime,
    step_microseconds: int,
    count: int,
    original_admission: Sequence[Tuple[int, int, bool]],
    station_profiles: Sequence[StationProfile]
) -> List[ChangeSegment]: ...
