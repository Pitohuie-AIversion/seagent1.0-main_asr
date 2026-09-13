"""Data provider implementation for Copernicus Marine Toolbox and Synthetic offline testing."""

from __future__ import annotations

import logging
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

from .contracts import CurrentForecastData, CurrentQuery


class ProviderError(Exception):
    """Exception raised by current providers with error code and retry flag."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


# Standard Copernicus 0.083deg vertical depth levels (meters, positive downwards)
COPERNICUS_STANDARD_DEPTHS = [
    0.49, 1.54, 2.65, 3.82, 5.08, 6.44, 7.93, 9.57, 11.41, 13.46,
    15.78, 18.39, 21.34, 24.69, 28.50, 32.84, 37.78, 43.41, 49.81, 57.08,
    65.34, 74.74, 85.44, 97.60, 111.41, 127.08, 144.82, 164.85, 187.41, 212.78,
    241.25, 273.18, 308.90, 348.78, 393.24, 442.69, 497.55, 558.26, 625.27, 699.07,
    780.14, 868.99, 966.17, 1072.23, 1187.67, 1312.98, 1448.65, 1595.12, 1752.79,
    1922.08, 2102.73, 2294.61, 2497.41, 2710.82, 2934.50, 3168.08, 3411.19, 3663.46,
    3924.51, 4193.99, 4471.55, 4756.84, 5049.50, 5349.19, 5655.57, 5968.29
]


def find_bounding_depth_layers(target_depth_m: float, depths: List[float]) -> List[float]:
    """Find exact matching layer or adjacent 2 bounding layers."""
    if target_depth_m < depths[0]:
        return [depths[0]]
    if target_depth_m >= depths[-1]:
        return [depths[-1]]

    for i in range(len(depths) - 1):
        d_lower = depths[i]
        d_upper = depths[i + 1]
        if math.isclose(target_depth_m, d_lower, abs_tol=1e-3):
            return [d_lower]
        if math.isclose(target_depth_m, d_upper, abs_tol=1e-3):
            return [d_upper]
        if d_lower < target_depth_m < d_upper:
            return [d_lower, d_upper]

    return [depths[-1]]


def generate_bounding_6h_time_nodes(start_time: datetime, end_time: datetime) -> List[datetime]:
    """Generate 6-hour native time nodes bounding the query interval."""
    # Convert to UTC
    st_utc = start_time.astimezone(timezone.utc)
    et_utc = end_time.astimezone(timezone.utc)

    # Floor start to nearest preceding 6h boundary (00:00, 06:00, 12:00, 18:00)
    floor_hour = (st_utc.hour // 6) * 6
    curr = st_utc.replace(hour=floor_hour, minute=0, second=0, microsecond=0)

    nodes = []
    while curr <= et_utc or len(nodes) < 2:
        nodes.append(curr)
        curr += timedelta(hours=6)

    # Ensure last node covers et_utc
    if nodes[-1] < et_utc:
        nodes.append(nodes[-1] + timedelta(hours=6))

    return nodes


def calculate_grid_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in kilometers between two geographic coordinates."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


class CopernicusProvider:
    """Production provider using Copernicus Marine Toolbox (lazy xarray subset)."""

    PRODUCT = "GLOBAL_ANALYSISFORECAST_PHY_001_024"
    DATASET = "cmems_mod_glo_phy-cur_anfc_0.083deg_PT6H-i"
    NATIVE_STEP_SECONDS = 21600  # 6 hours

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout_seconds: float = 30.0,
    ):
        self.username = username or os.getenv("COPERNICUSMARINE_SERVICE_USERNAME") or os.getenv("COPERNICUS_USERNAME")
        self.password = password or os.getenv("COPERNICUSMARINE_SERVICE_PASSWORD") or os.getenv("COPERNICUS_PASSWORD")
        self.timeout_seconds = timeout_seconds

    def check_credentials(self) -> None:
        """Enforce strict non-interactive check: fail fast if unconfigured."""
        if not self.username or not self.password:
            logger.error(
                "Copernicus credentials missing: please set COPERNICUSMARINE_SERVICE_USERNAME and COPERNICUSMARINE_SERVICE_PASSWORD in environment."
            )
            raise ProviderError(
                code="NOT_CONFIGURED",
                message="当前海流预测数据服务未配置凭据，暂时无法评估作业窗口。",
                retryable=False,
            )

    def fetch(self, query: CurrentQuery) -> CurrentForecastData:
        """Fetch real data from Copernicus Marine API."""
        self.check_credentials()

        try:
            import copernicusmarine
        except ImportError:
            raise ProviderError(
                code="PROVIDER_ERROR",
                message="The 'copernicusmarine' Python package is not installed in the environment.",
                retryable=False,
            )

        # Ensure spatial bounds
        if not (-80.0 <= query.latitude <= 90.0):
            raise ProviderError("OUT_OF_BOUNDS", f"Latitude {query.latitude} is outside valid dataset coverage (-80 to 90)")

        # Query live dataset
        try:
            import xarray as xr

            # Calculate bounding 6h time nodes
            time_nodes = generate_bounding_6h_time_nodes(query.start_time, query.end_time)
            depth_layers = find_bounding_depth_layers(query.operation_depth_m, COPERNICUS_STANDARD_DEPTHS)

            # Snap to 0.083 deg grid
            res = 0.0833333
            grid_lat = round(query.latitude / res) * res
            grid_lon = round(query.longitude / res) * res
            dist_km = calculate_grid_distance_km(query.latitude, query.longitude, grid_lat, grid_lon)

            # Open dataset lazily
            ds = copernicusmarine.open_dataset(
                dataset_id=self.DATASET,
                username=self.username,
                password=self.password,
            )

            # Subset space, time and depth lazily before loading
            subset = ds[["uo", "vo"]].sel(
                latitude=grid_lat,
                longitude=grid_lon,
                method="nearest",
            ).sel(
                depth=depth_layers,
                method="nearest",
            ).sel(
                time=slice(time_nodes[0], time_nodes[-1]),
            )

            # Load only the subsetted tiny slice
            loaded = subset.load()

            actual_time_steps = [
                datetime.fromisoformat(str(t)).replace(tzinfo=timezone.utc)
                for t in loaded.time.values
            ]
            actual_depths = [float(d) for d in loaded.depth.values]

            uo_raw = loaded.uo.values
            vo_raw = loaded.vo.values

            # Check for NaN / land mask
            if bool(xr.ufuncs.isnan(loaded.uo).any()) or bool(xr.ufuncs.isnan(loaded.vo).any()):
                raise ProviderError(
                    code="LAND_OR_INVALID",
                    message=f"Location ({query.latitude}, {query.longitude}) is over land or has missing current data.",
                    retryable=False,
                )

            # Convert numpy to python list [time][depth]
            uo_matrix = uo_raw.tolist()
            vo_matrix = vo_raw.tolist()

            # Ensure 2D shape
            if len(actual_depths) == 1 and isinstance(uo_matrix[0], float):
                uo_matrix = [[val] for val in uo_matrix]
                vo_matrix = [[val] for val in vo_matrix]

            model_run_val = None
            if "forecast_reference_time" in loaded.coords:
                frt = loaded.coords["forecast_reference_time"].values
                model_run_val = datetime.fromisoformat(str(frt)).replace(tzinfo=timezone.utc)

            return CurrentForecastData(
                request_fingerprint=query.fingerprint,
                snapshot_id=f"snap_{uuid.uuid4().hex[:12]}",
                provider="copernicus_marine",
                is_synthetic=False,
                product=self.PRODUCT,
                dataset=self.DATASET,
                variables=["uo", "vo"],
                retrieved_at=datetime.now(timezone.utc),
                model_run=model_run_val,
                actual_grid_latitude=float(loaded.latitude.values),
                actual_grid_longitude=float(loaded.longitude.values),
                grid_distance_km=dist_km,
                native_time_steps=actual_time_steps,
                native_depth_layers_m=actual_depths,
                uo=uo_matrix,
                vo=vo_matrix,
                warnings=[],
            )

        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                code="PROVIDER_ERROR",
                message=f"Copernicus data retrieval failed: {exc}",
                retryable=True,
            ) from exc


class SyntheticCopernicusProvider:
    """Deterministic synthetic current fixture for offline algorithmic testing.

    Generates synthetic test data with periodicity and depth variation for algorithm
    and contract verification. Does NOT represent real ocean conditions.
    """

    PRODUCT = "GLOBAL_ANALYSISFORECAST_PHY_001_024"
    DATASET = "cmems_mod_glo_phy-cur_anfc_0.083deg_PT6H-i"
    NATIVE_STEP_SECONDS = 21600  # 6 hours

    def __init__(self, force_error: Optional[str] = None, base_speed_mps: float = 0.45):
        self.force_error = force_error
        self.base_speed_mps = base_speed_mps

    def fetch(self, query: CurrentQuery) -> CurrentForecastData:
        """Generate synthetic ocean current data respecting Copernicus 6h grid."""
        if self.force_error:
            if self.force_error == "NOT_CONFIGURED":
                raise ProviderError("NOT_CONFIGURED", "Copernicus credentials not configured in environment.", False)
            if self.force_error == "OUT_OF_BOUNDS":
                raise ProviderError("OUT_OF_BOUNDS", "Target coordinates out of bounds.", False)
            if self.force_error == "LAND_OR_INVALID":
                raise ProviderError("LAND_OR_INVALID", "Target location falls on land.", False)
            if self.force_error == "MISSING_DATA":
                raise ProviderError("MISSING_DATA", "Dataset has missing observations.", False)
            if self.force_error == "TIMEOUT":
                raise ProviderError("TIMEOUT", "Provider request timed out.", True)
            raise ProviderError("PROVIDER_ERROR", f"Simulated error: {self.force_error}", False)

        # Check bounds
        if not (-80.0 <= query.latitude <= 90.0):
            raise ProviderError("OUT_OF_BOUNDS", f"Latitude {query.latitude} out of bounds")

        # Snap to 0.08333 deg grid
        res = 0.0833333
        grid_lat = round(query.latitude / res) * res
        grid_lon = round(query.longitude / res) * res
        dist_km = calculate_grid_distance_km(query.latitude, query.longitude, grid_lat, grid_lon)

        # Depth selection
        depth_layers = find_bounding_depth_layers(query.operation_depth_m, COPERNICUS_STANDARD_DEPTHS)

        # Time nodes (6h intervals)
        time_nodes = generate_bounding_6h_time_nodes(query.start_time, query.end_time)

        # Generate realistic physical velocity profiles:
        # 1. Semidiurnal M2 tidal current (T = 12.42h)
        # 2. Steady background geostrophic flow
        # 3. Depth attenuation: exp(-depth / 350)
        uo_matrix: List[List[float]] = []
        vo_matrix: List[List[float]] = []

        omega_m2 = 2.0 * math.pi / (12.42 * 3600.0)
        phase_lat = math.radians(grid_lat * 10.0)

        for t in time_nodes:
            t_sec = t.timestamp()
            row_u = []
            row_v = []
            for d in depth_layers:
                atten = math.exp(-d / 400.0)
                # East-west tidal ellipse
                u_val = (self.base_speed_mps * 0.75 * math.cos(omega_m2 * t_sec + phase_lat) + 0.15) * atten
                # North-south tidal ellipse with 90 deg phase lag
                v_val = (self.base_speed_mps * 0.55 * math.sin(omega_m2 * t_sec + phase_lat) + 0.05) * atten
                row_u.append(round(u_val, 4))
                row_v.append(round(v_val, 4))
            uo_matrix.append(row_u)
            vo_matrix.append(row_v)

        return CurrentForecastData(
            request_fingerprint=query.fingerprint,
            snapshot_id=f"snap_synth_{uuid.uuid4().hex[:10]}",
            provider="synthetic_test_fixture",
            is_synthetic=True,
            product=self.PRODUCT,
            dataset=self.DATASET,
            variables=["uo", "vo"],
            retrieved_at=datetime.now(timezone.utc),
            model_run=datetime(2026, 9, 12, 0, 0, 0, tzinfo=timezone.utc),
            actual_grid_latitude=round(grid_lat, 4),
            actual_grid_longitude=round(grid_lon, 4),
            grid_distance_km=round(dist_km, 2),
            native_time_steps=time_nodes,
            native_depth_layers_m=depth_layers,
            uo=uo_matrix,
            vo=vo_matrix,
            warnings=["SYNTHETIC_DATA_TEST_FIXTURE_NOT_FOR_PRODUCTION"],
        )
