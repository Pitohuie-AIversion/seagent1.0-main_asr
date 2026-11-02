import pytest
from src.extraction.coord_parser import _in_coord_range

@pytest.mark.parametrize(
    "lat, lon, expected",
    [
        # Valid coordinates (Happy path)
        (0.0, 0.0, True),          # Equator / Prime Meridian
        (45.0, 90.0, True),        # Middle values
        (90.0, 180.0, True),       # Maximum boundaries
        (-90.0, -180.0, True),     # Minimum boundaries
        (90.0, -180.0, True),      # Mixed boundaries
        (-90.0, 180.0, True),      # Mixed boundaries

        # Invalid latitude
        (90.1, 0.0, False),        # Just above max lat
        (-90.1, 0.0, False),       # Just below min lat
        (100.0, 0.0, False),       # Way out lat
        (-100.0, 0.0, False),      # Way out lat

        # Invalid longitude
        (0.0, 180.1, False),       # Just above max lon
        (0.0, -180.1, False),      # Just below min lon
        (0.0, 200.0, False),       # Way out lon
        (0.0, -200.0, False),      # Way out lon

        # Both invalid
        (91.0, 181.0, False),      # Both just above max
        (-91.0, -181.0, False),    # Both just below min
        (100.0, 200.0, False),     # Both way out
    ]
)
def test_in_coord_range(lat, lon, expected):
    """
    Tests the _in_coord_range function to ensure it correctly identifies
    whether a latitude and longitude pair is within valid bounds.
    """
    assert _in_coord_range(lat, lon) is expected
