HEADER = "TradDt,TckrSymb,SctySrs,ClsPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal\n"


def make_csv(day, prices, pad=600):
    """prices: {symbol: (close, prev_close, volume)}; pad adds filler EQ rows."""
    lines = [f"{day},{s},EQ,{c},{p},{v},{c * v}" for s, (c, p, v) in prices.items()]
    lines += [f"{day},PAD{i:04d},EQ,10.0,10.0,1000,10000" for i in range(pad)]
    return HEADER + "\n".join(lines) + "\n"
