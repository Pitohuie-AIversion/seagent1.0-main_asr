"""SEAgent mathematical processing: depth and time linear interpolation and maximum current speed."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from typing import List, Optional, Tuple

from .contracts import CurrentForecastData


@dataclass(frozen=True)
class InterpolatedPoint:
    """Calculated ocean current vector and scalar speed at specific time and depth."""

    timestamp: datetime
    u_mps: float
    v_mps: float
    speed_mps: float
    is_native_node: bool = False


class CurrentProcessor:
    """Pure mathematical processing of ocean current matrices (depth and time interpolation)."""

    @staticmethod
    def _verify_forecast_security(forecast: CurrentForecastData) -> None:
        """Fail-closed security check: synthetic data must NEVER be processed outside explicit test environment."""
        if getattr(forecast, "is_synthetic", False):
            env_mode = os.getenv("SEAGENT_CURRENT_ENV", "production").lower()
            if env_mode != "test":
                raise RuntimeError(
                    f"Security Violation: Attempted to process synthetic current forecast in {env_mode} environment. "
                    "Synthetic fixtures are strictly restricted to SEAGENT_CURRENT_ENV=test profile."
                )

    @classmethod
    def interpolate_depth_at_nodes(
        cls,
        forecast: CurrentForecastData,
        target_depth_m: float,
    ) -> List[Tuple[datetime, float, float]]:
        """Perform linear interpolation across depth layers at each native time node.

        Returns list of (native_timestamp, u_interpolated, v_interpolated).
        """
        cls._verify_forecast_security(forecast)
        depths = forecast.native_depth_layers_m
        time_steps = forecast.native_time_steps
        uo = forecast.uo
        vo = forecast.vo

        results = []

        if len(depths) == 1:
            # Exact match or single boundary layer
            for i, t in enumerate(time_steps):
                results.append((t, uo[i][0], vo[i][0]))
            return results

        # 2 layers: [d0, d1]
        d0, d1 = depths[0], depths[1]
        if math.isclose(d1, d0, abs_tol=1e-5):
            w0, w1 = 1.0, 0.0
        else:
            # Clamp weight to [0, 1]
            w1 = max(0.0, min(1.0, (target_depth_m - d0) / (d1 - d0)))
            w0 = 1.0 - w1

        for i, t in enumerate(time_steps):
            u_val = w0 * uo[i][0] + w1 * uo[i][1]
            v_val = w0 * vo[i][0] + w1 * vo[i][1]
            results.append((t, u_val, v_val))

        return results

    @classmethod
    def evaluate_at_time(
        cls,
        forecast: CurrentForecastData,
        target_depth_m: float,
        target_time: datetime,
    ) -> InterpolatedPoint:
        """Evaluate (u, v, speed) at arbitrary target_time via piecewise linear vector interpolation."""
        depth_nodes = cls.interpolate_depth_at_nodes(forecast, target_depth_m)
        t_target_utc = target_time.astimezone(timezone.utc)

        t_first = depth_nodes[0][0].astimezone(timezone.utc)
        t_last = depth_nodes[-1][0].astimezone(timezone.utc)

        if t_target_utc < t_first or t_target_utc > t_last:
            raise ValueError(
                f"Target time {target_time.isoformat()} is outside available forecast range "
                f"[{t_first.isoformat()}, {t_last.isoformat()}]"
            )

        # Exact match with a native node
        for t_node, u_val, v_val in depth_nodes:
            if t_node.astimezone(timezone.utc) == t_target_utc:
                speed = math.hypot(u_val, v_val)
                return InterpolatedPoint(
                    timestamp=t_target_utc,
                    u_mps=u_val,
                    v_mps=v_val,
                    speed_mps=speed,
                    is_native_node=True,
                )

        # Piecewise linear interpolation between adjacent bounding nodes
        for i in range(len(depth_nodes) - 1):
            t0, u0, v0 = depth_nodes[i]
            t1, u1, v1 = depth_nodes[i + 1]
            t0_utc = t0.astimezone(timezone.utc)
            t1_utc = t1.astimezone(timezone.utc)

            if t0_utc <= t_target_utc <= t1_utc:
                dt_total = (t1_utc - t0_utc).total_seconds()
                alpha = (t_target_utc - t0_utc).total_seconds() / dt_total
                u_interp = u0 + alpha * (u1 - u0)
                v_interp = v0 + alpha * (v1 - v0)
                speed = math.hypot(u_interp, v_interp)
                return InterpolatedPoint(
                    timestamp=t_target_utc,
                    u_mps=u_interp,
                    v_mps=v_interp,
                    speed_mps=speed,
                    is_native_node=False,
                )

        raise RuntimeError(f"Failed to interpolate at {target_time}")

    @classmethod
    def find_interval_max_speed(
        cls,
        forecast: CurrentForecastData,
        target_depth_m: float,
        start_time: datetime,
        end_time: datetime,
    ) -> Tuple[float, InterpolatedPoint]:
        """Compute the mathematically exact maximum speed in [start_time, end_time].

        Mathematical proof:
        On any linear segment in (u, v) space parameterized by t in [0, 1]:
            u(t) = (1 - t)*u0 + t*u1
            v(t) = (1 - t)*v0 + t*v1
        The squared norm S(t) = u(t)^2 + v(t)^2 is a quadratic polynomial with non-negative
        second derivative S''(t) = 2*(u1 - u0)^2 + 2*(v1 - v0)^2 >= 0.
        Therefore, the speed function V(t) = sqrt(S(t)) is convex along the segment.
        The maximum of a convex function over any closed sub-interval is attained at its endpoints.
        Hence, the maximum speed over [start_time, end_time] MUST occur at either:
            1. start_time
            2. end_time
            3. one of the internal native time nodes t_k in (start_time, end_time).
        """
        depth_nodes = cls.interpolate_depth_at_nodes(forecast, target_depth_m)
        st_utc = start_time.astimezone(timezone.utc)
        et_utc = end_time.astimezone(timezone.utc)

        t_first = depth_nodes[0][0].astimezone(timezone.utc)
        t_last = depth_nodes[-1][0].astimezone(timezone.utc)

        if st_utc < t_first or et_utc > t_last:
            raise ValueError(
                f"Query interval [{start_time.isoformat()}, {end_time.isoformat()}] exceeds "
                f"forecast bounds [{t_first.isoformat()}, {t_last.isoformat()}]"
            )

        # Collect critical evaluation points: endpoints + internal native nodes
        eval_points: List[InterpolatedPoint] = []

        # 1. Start time
        eval_points.append(cls.evaluate_at_time(forecast, target_depth_m, st_utc))

        # 2. Internal native nodes strictly between start and end
        for t_node, u_val, v_val in depth_nodes:
            t_utc = t_node.astimezone(timezone.utc)
            if st_utc < t_utc < et_utc:
                speed = math.hypot(u_val, v_val)
                eval_points.append(
                    InterpolatedPoint(
                        timestamp=t_utc,
                        u_mps=u_val,
                        v_mps=v_val,
                        speed_mps=speed,
                        is_native_node=True,
                    )
                )

        # 3. End time
        eval_points.append(cls.evaluate_at_time(forecast, target_depth_m, et_utc))

        # Find point with highest speed
        max_point = max(eval_points, key=lambda p: p.speed_mps)
        return max_point.speed_mps, max_point
