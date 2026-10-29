import matplotlib.pyplot as plt

class Plot:
    def __init__(self, width: float, height: float):
        # figsize is in inches, not pixels
        self.fig, self.ax = plt.subplots(figsize=(width, height))

    def scatter(self, xs: list[float], ys: list[float], color="blue", size=20, **kwargs):
        self.ax.scatter(xs, ys, c=color, s=size, **kwargs)
        return self

    def line(self, xs: list[float], ys: list[float], color="blue", width=1.5, **kwargs):
        self.ax.plot(xs, ys, color=color, linewidth=width, **kwargs)
        return self

    def contour(self, xs, ys, zs, **kwargs):
        self.ax.contourf(xs, ys, zs, **kwargs)
        return self

    def show(self):
        plt.show()

    def save(self, path: str):
        self.fig.savefig(path)
