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
            p_idx = (len(s) - 1) * (p/100)
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

#jitter estimator following RFC3550
class Jitter:
    def __init__(self):
        self.jitter = 0.0 #jitter estimate
        self._prev = None #previous transmit time
        self.lpf = 16.0 #low pass filter (smooths jitter over time - RFC3350)

    def add(self, transmit_ms: float):
        if self._prev is None:
            self._prev = transmit_ms
            return
        diff = abs(transmit_ms - self._prev)
        self.jitter += (diff - self.jitter) / self.lpf
        self._prev = transmit_ms

    def value(self):
        return self.jitter
