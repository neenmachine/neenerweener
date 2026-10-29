from engine.value import Value
import numpy as np

def fit():
    w,b = Value(0), Value(0)
    steps = 1500
    xsteps = 100
    lr = 0.1
    xrange = np.linspace(0, 1, num=xsteps)

    for step in range(steps):
        error = Value(0)
        for x in xrange:
            yp = w*x+b
            y = 3*x+2
            error += (y-yp)*(y-yp)
        mse = error / xsteps
        mse.backwards()

        w.data -= w.grad*lr
        b.data -= b.grad*lr
        w.grad = 0
        b.grad = 0

    print(w, b)

if __name__ == "__main__":
    fit()
