LEVELS = ("easy", "medium", "hard")

# Based on how many distinct shapes/details a player needs to draw.
EASY_CLASSES = set("""
apple banana book candle carrot clock cloud crown cup diamond door donut
dumbbell envelope fish flower house ladder mushroom pizza tree umbrella
""".split())
HARD_CLASSES = set("""
bicycle bridge castle church drums elephant flamingo giraffe guitar hedgehog
helicopter hurricane jail kangaroo lighthouse
""".split())


def default_difficulties(classes: list[str]) -> dict[str, str]:
    return {
        name: "easy" if name in EASY_CLASSES else
        "hard" if name in HARD_CLASSES else "medium"
        for name in classes
    }


def round_difficulty(round_number: int) -> str:
    return LEVELS[min(max(round_number - 1, 0) // 2, 2)]
