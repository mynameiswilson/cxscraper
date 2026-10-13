"""USAC cyclocross upgrade points, transcribed from assets.usacycling.org
prod/documents/050726_CX.png (UPGRADE POINTS FOR MASS START RACES)."""

# (min_starters, max_starters) -> {place: points}
BRACKETS = [
    ((5, 10),   {1: 3, 2: 2, 3: 1}),
    ((11, 20),  {1: 4, 2: 3, 3: 2, 4: 1}),
    ((21, 35),  {1: 5, 2: 4, 3: 3, 4: 2, 5: 1}),
    ((36, 60),  {1: 8, 2: 6, 3: 5, 4: 4, 5: 3, 6: 2, 7: 1}),
    ((61, 80),  {1: 10, 2: 8, 3: 7, 4: 6, 5: 5, 6: 4, 7: 3, 8: 2, 9: 1}),
    ((81, 10**6), {1: 11, 2: 10, 3: 9, 4: 8, 5: 7, 6: 6, 7: 5, 8: 4, 9: 3, 10: 2, 11: 1}),
]

# Voluntary upgrade thresholds, 36-month window.
VOLUNTARY = {"Category 5": ("Category 4", None),   # "Finish 5 Races" - races, not points
             "Category 4": ("Category 3", 10),
             "Category 3": ("Category 2", 15),
             "Category 2": ("Category 1", 20)}


def points_for(place, starters):
    """Upgrade points for a finishing place in a field of `starters`."""
    if not isinstance(place, int) or place < 1:
        return 0
    if starters < 5:
        return 0
    for (lo, hi), table in BRACKETS:
        if lo <= starters <= hi:
            return table.get(place, 0)
    return 0


if __name__ == "__main__":
    # Spot-check against the chart's printed cells.
    checks = [
        (1, 8, 3), (2, 8, 2), (3, 8, 1), (4, 8, 0),
        (1, 15, 4), (4, 15, 1), (5, 15, 0),
        (1, 28, 5), (5, 28, 1), (6, 28, 0),
        (1, 50, 8), (7, 50, 1), (8, 50, 0),
        (1, 70, 10), (9, 70, 1), (10, 70, 0),
        (1, 100, 11), (11, 100, 1), (12, 100, 0),
        (1, 4, 0),   # under 5 starters pays nothing
    ]
    bad = [(p, s, e, points_for(p, s)) for p, s, e in checks if points_for(p, s) != e]
    print("table spot-checks:", "all pass" if not bad else f"FAIL {bad}")
    print("bracket boundaries:", [(f"{lo}-{hi}", points_for(1, lo), points_for(1, hi))
                                  for (lo, hi), _ in BRACKETS])
