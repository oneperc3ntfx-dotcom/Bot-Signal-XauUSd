def detect_structure(candles):
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]

    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "BULLISH"
    elif highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "BEARISH"
    return "RANGE"


def detect_choch(candles):
    if len(candles) < 3:
        return None

    if candles[-1]["c"] > candles[-2]["h"]:
        return "BULLISH CHOCH"

    if candles[-1]["c"] < candles[-2]["l"]:
        return "BEARISH CHOCH"

    return None
