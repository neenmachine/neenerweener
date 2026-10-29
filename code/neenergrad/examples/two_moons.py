import numpy as np
from typing import List
from data.dataset import Dataset
from visualization.plot import Plot

from engine.nn import MLP
from engine.value import Value

def accuracy(mlp: MLP, moons: List[tuple[float, float, int]]):
    cnt = 0
    for x, y, label in moons:
        predicted = 1 if (mlp([Value(x),Value(y)])[0].data > 0) else -1
        cnt += (predicted == label)
    return float(cnt) / len(moons)

def train(moons: List[tuple[float, float, int]], steps: int = 100):
    N = len(moons)
    # two moons
    # MLP: 2 -> 16 -> 16 -> 1
    # 2 inputs: (x,y)
    # 1 output: predicted label
    mlp = MLP(
                [2,16,16,1],
                -1.0,
                +1.0,
                [Value.tanh, Value.tanh, None]
             )
    
    lr = 0.1
    for k in range(steps):
        error = Value(0)
        for j in range(N):
            x, y, label = moons[j]
            output = mlp([Value(x), Value(y)])[0]
            error += (output-label) * (output-label)
        MSE = error / (N)
        MSE.backwards()
        params = mlp.parameters()
        for x in params:
            x.data -= x.grad * lr
            x.grad = 0
    return mlp

def f():
    plot = Plot(6,6)
    moons = Dataset.make_moons(100)
    mlp = train(moons, 10)

    xs, ys, labels = zip(*moons)
    x,y,z = np.linspace(min(xs), max(xs), 100), np.linspace(min(ys), max(ys), 100), []
    px, py = np.meshgrid(x,y)

    for itx in range(len(px)):
        z.append([])
        for ity in range(len(px[itx])):
            pointx, pointy = px[itx][ity], py[itx][ity]
            score = mlp([pointx, pointy])[0].data
            z[itx].append(score)
    plot.contour(px,py,z,levels=[-100,0,+100], cmap="coolwarm", alpha=0.3)
    plot.scatter(xs, ys, color=labels, cmap="coolwarm")
    plot.show()

    print(f"accuracy: {accuracy(mlp, moons) * 100.0}%")
    plot.save("/tmp/two_moons.png")
    
if __name__ == "__main__":
    f()
