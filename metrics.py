import math
from collections import deque

#helper to store recent samples and compute averages + percentiles
class RollingStats:
    def __init__(self, maxlen=2048):
        self.samples = deque(maxlen=maxlen)

    def add(self, x: float):
        self.samples.append(x)

    #percentiles we want: 50th 95th
    def percentiles(self, ps=(50, 95)):
        if not self.samples:
            return {p: float('nan') for p in ps}
        s = sorted(self.samples)
        #find smaple at pth percentile pos
        def pct(p):
            p_idx = len(s) - 1 * (p/100)
            #split the (i)nteger|(frac)tion
            frac, i = math.modf(p_idx)
            i = int(i)
            if frac==0:
                return s[i]
            #if there is a frac, interpolate between surrounding samples
            return s[i] * (1-frac) + s[min(i+1, len(s)-1)] * frac
        return {p: pct(p) for p in ps}

    def avg(self):
        return sum(self.samples) / len(self.samples) if self.samples else float('nan')

