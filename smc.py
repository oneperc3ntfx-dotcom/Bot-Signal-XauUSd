def smc_engine(candles):

    structure = detect_structure(candles)
    choch = detect_choch(candles)
    bos = detect_bos(candles)
    sweep = detect_liquidity_sweep(candles)
    ob = detect_order_block(candles)
    fvg = detect_fvg(candles)

    reasons = []

    # STRUCTURE
    reasons.append(f"Structure: {structure}")

    # CHOCH
    if choch:
        reasons.append(f"CHOCH detected: {choch}")

    # BOS
    if bos:
        reasons.append(f"BOS confirmed: {bos}")

    # LIQUIDITY
    if sweep:
        reasons.append(f"Liquidity event: {sweep}")

    # ORDER BLOCK
    if ob:
        reasons.append(f"Order Block: {ob}")

    # FVG
    if fvg:
        reasons.append(f"FVG zone: {fvg}")

    # BIAS DECISION
    if "BULLISH" in str([structure, choch, bos, sweep, ob, fvg]):
        bias = "BULLISH"
    elif "BEARISH" in str([structure, choch, bos, sweep, ob, fvg]):
        bias = "BEARISH"
    else:
        bias = "RANGE"

    return bias, reasons
