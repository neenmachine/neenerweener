import numpy as np
from engine.tensor import Tensor

class SGD:
    def __init__(self, lr: float = 0.1, params: List[Tensor] = None):
        self.lr = lr
        self.params = params if params else []
    
    def step(self):
        for tensor in self.params:
            tensor.data -= tensor.grad * self.lr

    def zero_grad(self):
        for tensor in self.params:
            tensor.grad = np.zeros(tensor.shape)

class Momentum:
    def __init__(self, lr: float = 0.1, mm: float = 0.9, params: List[Tensor] = None):
        self.lr = lr
        self.momentum = mm
        self.params = params if params else []
        self.state = []

        for tensor in self.params:
            self.state.append(np.zeros(tensor.shape))

    def step(self):
        for idx, tensor in enumerate(self.params):
            b = self.state[idx] * self.momentum + tensor.grad
            tensor.data -= self.lr * b
            self.state[idx] = b

    def zero_grad(self):
        for tensor in self.params:
            tensor.grad = np.zeros(tensor.shape)

class Adam:
    def __init__(self, lr: float = 0.001, b1: float = 0.9, b2: float = 0.999, eps: float = 1e-8, params: List[Tensor] = None):
        self.lr = lr
        self.b1 = b1
        self.b2 = b2
        self.eps = eps
        self.t = 0

        self.params = params if params else []
        self.m = []
        self.v = []

        for tensor in self.params:
            self.m.append(np.zeros(tensor.shape))
            self.v.append(np.zeros(tensor.shape))

    def step(self):
        self.t += 1
        
        for idx, tensor in enumerate(self.params):
            self.m[idx] = self.b1 * self.m[idx] + (1 - self.b1) * tensor.grad
            self.v[idx] = self.b2 * self.v[idx] + (1 - self.b2) * tensor.grad * tensor.grad

            mhat = self.m[idx] / (1 - self.b1 ** self.t)
            vhat = self.v[idx] / (1 - self.b2 ** self.t)

            tensor.data -= self.lr * mhat / (np.sqrt(vhat) + self.eps)


    def zero_grad(self):
        for tensor in self.params:
            tensor.grad = np.zeros(tensor.shape)
