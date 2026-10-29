import numpy as np
import math
import random
from typing import List

class Dataset:
    def __init__(self):
        pass

    @classmethod
    def make_moons(cls, points: int, noise: bool = True) -> List[tuple[float, float, int]]:
        trange = np.linspace(0, math.pi, num=points//2)

        m0, m1 = [], []
        for t in trange:
            sin_t, cos_t = math.sin(t), math.cos(t)
            p0x, p0y = cos_t, sin_t
            p1x, p1y = 1-cos_t, 0.5-sin_t

            if noise:
                p0x += random.uniform(-0.1, +0.1)
                p0y += random.uniform(-0.1, +0.1)
                p1x += random.uniform(-0.1, +0.1)
                p1y += random.uniform(-0.1, +0.1)
            m0.append((p0x, p0y, +1))
            m1.append((p1x, p1y, -1))
        return m0 + m1
