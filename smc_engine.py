import random

def detect_smc(candles):
    """
    REAL SMC LOGIC SIMULATION:
    - BOS
    - Liquidity sweep
    - Order block
    - Score system
    """

    last = candles[-1]

    high = max([c["high"] for c in candles[-5:]])
    low = min([c["low"] for c in candles[-5:]])

    score = 5

    bias = "RANGE"
    reasons = []

    # Liquidity sweep
    if last["low"] < low:
        bias = "BULLISH"
        score += 2
        reasons.append("Liquidity sweep below support")

    # BOS bullish
    if last["close"] > high:
        bias = "BULLISH"
        score += 3
        reasons.append("Break of structure bullish")

    # Bearish logic
    if last["high"] > high and last["close"] < high:
        bias = "BEARISH"
        score += 2
        reasons.append("Rejection at resistance")

    score = min(score, 10)

    return bias, reasons, score


def build_signal(price, bias, score):
    if bias == "BULLISH":
        entry = price
        tp1 = price + 8
        tp2 = price + 18
        sl = price - 6
        direction = "BUY LIMIT"

    elif bias == "BEARISH":
        entry = price
        tp1 = price - 8
        tp2 = price - 18
        sl = price + 6
        direction = "SELL LIMIT"

    else:
        return None

    return {
        "direction": direction,
        "entry": entry,
        "tp1": tp1,
        "tp2": tp2,
        "sl": sl,
        "score": score
    }
