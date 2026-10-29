import numpy as np
import numpy.typing as npt
from collections import defaultdict

class Tensor:
    def __init__(self, data: npt.ArrayLike, _children: set[Tensor] = None):
        self.data = np.asarray(data, dtype=np.float64)
        self.children = set(_children) if _children else set()
        self.shape = self.data.shape
        self.grad = np.zeros(self.shape)
        self._backward = lambda: None

    def __add__(self, other: Tensor) -> Tensor:
        out = Tensor(self.data + other.data, (self, other))

        def _backward():
            self.grad += self.unbroadcast(out.grad, self.shape)
            other.grad += self.unbroadcast(out.grad, other.shape)
        out._backward = _backward
        return out

    def __mul__(self, other: Tensor) -> Tensor:
        out = Tensor(self.data * other.data, (self, other))

        def _backward():
            self.grad += self.unbroadcast(out.grad * other.data, self.shape)
            other.grad += self.unbroadcast(out.grad * self.data, other.shape)
        out._backward = _backward
        return out

    def __sub__(self, other: Tensor) -> Tensor:
        out = Tensor(self.data - other.data, (self, other))

        def _backward():
            self.grad += self.unbroadcast(out.grad, self.shape)
            other.grad += self.unbroadcast(-out.grad, other.shape)
        out._backward = _backward
        return out

    def __pow__(self, exp: int) -> Tensor:
        out = Tensor(np.power(self.data, exp), (self,))

        def _backward():
            self.grad += self.unbroadcast(out.grad * exp * np.power(self.data, exp-1), self.shape)
        out._backward = _backward
        return out

    def __truediv__(self, other: Tensor) -> Tensor:
        out = Tensor(self.data / other.data, (self, other))

        def _backward():
            self.grad += self.unbroadcast(out.grad / other.data, self.shape)
            other.grad += self.unbroadcast(out.grad * (-self.data / (other.data * other.data)), other.shape)
        out._backward = _backward
        return out

    def __matmul__(self, other: Tensor) -> Tensor:
        out = Tensor(self.data @ other.data, (self, other))

        def _backward():
            self.grad += self.unbroadcast(out.grad @ np.swapaxes(other.data, -1, -2), self.shape)
            other.grad += self.unbroadcast(np.swapaxes(self.data, -1, -2) @ out.grad, other.shape)
        out._backward = _backward
        return out

    def exp(self) -> Tensor:
        x = np.exp(self.data)
        out = Tensor(x, (self,))

        def _backward():
            self.grad += self.unbroadcast(out.grad * x, self.shape)
        out._backward = _backward
        return out

    def log(self) -> Tensor:
        assert(np.all(self.data > 0))
        out = Tensor(np.log(self.data), (self,))

        def _backward():
            self.grad += self.unbroadcast(out.grad / self.data, self.shape)
        out._backward = _backward
        return out

    def tanh(self) -> Tensor:
        x = np.tanh(self.data)
        out = Tensor(x, (self,))

        def _backward():
            self.grad += self.unbroadcast(out.grad * (np.full(self.shape, 1) - x*x), self.shape)
        out._backward = _backward
        return out

    def relu(self) -> Tensor:
        out = Tensor(np.maximum(0, self.data), (self,))

        def _backward():
            self.grad += self.unbroadcast(out.grad * (self.data > 0).astype(float), self.shape)
        out._backward = _backward
        return out

    def __neg__(self) -> Tensor:
        return self.__mul__(Tensor.fill(self.shape, -1))

    def __repr__(self) -> str:
        return f"Tensor(shape={self.shape}, data={self.data})"

    def sum(self, axis: None|int|tuple[int,...] = None, keepdims: bool = False) -> Tensor:
        out = Tensor(self.data.sum(axis=axis, keepdims=keepdims), (self,))

        def _backward():
            grad = np.expand_dims(out.grad, axis) if not keepdims and axis is not None else out.grad
            self.grad += grad * np.ones(self.shape)

        out._backward = _backward
        return out

    def mean(self, axis: None|int|tuple[int,...] = None, keepdims: bool = False) -> Tensor:
        t0 = self.sum(axis, keepdims)
        t1 = Tensor(self.data.size/t0.data.size)

        return t0/t1

    def reshape(self, shape: tuple[int, ...]) -> Tensor:
        out = Tensor(np.reshape(self.data, shape), (self,))

        def _backward():
            self.grad += out.grad.reshape(self.shape)
        out._backward = _backward
        return out

    def transpose(self, swap: tuple[int, ...]) -> Tensor:
        out = Tensor(np.transpose(self.data, swap), (self,))

        def _backward():
            self.grad += np.transpose(out.grad, np.argsort(swap))
        out._backward = _backward
        return out

    # logsoftmax(x_j) = x_j - log(sum(exp(x)))
    def log_softmax(self) -> Tensor:
        c = Tensor(np.max(self.data, axis=-1, keepdims=True))
        const = (self - c).exp().sum(axis=-1, keepdims=True).log()
        return (self - c) - const

    # assert labels and batch size are the same
    def cross_entropy(self, labels: List[int]) -> Tensor:
        assert(len(labels) == self.shape[0])

        one_hot = Tensor(np.eye(self.shape[-1])[labels])
        return -((self.log_softmax() * one_hot).sum(axis=-1, keepdims=False)).mean()

    def topo(self) -> List[Tensor]:
        visited = defaultdict(bool)
        ll = []

        def dfs(node: Tensor):
            if visited[node]:
                return
            
            for child in node.children:
                dfs(child)
            ll.append(node)
            visited[node] = True
        dfs(self)
        return ll[::-1]

    def backwards(self) -> None:
        ll = self.topo()

        self.grad = np.ones(self.shape)
        for tensor in ll:
            tensor._backward()

    @classmethod
    def unbroadcast(cls, grad: np.ndarray, cshape: tuple[int, ...]):
        L = len(cshape)
        while len(grad.shape) > L:
            grad = grad.sum(axis=0)

        for axis in range(L):
            if cshape[axis] == 1:
                grad = grad.sum(axis=axis, keepdims=True)

        return grad

    @classmethod
    def zeros(cls, shape: tuple[int, ...]) -> Tensor:
        return Tensor(np.zeros(shape))

    @classmethod
    def fill(cls, shape: tuple[int, ...], value: float) -> Tensor:
        return Tensor(np.full(shape, value))

    @classmethod
    def random(cls, shape: tuple[int, ...], lower: float = -1.0, upper: float = +1.0) -> Tensor:
        rng = np.random.default_rng()
        return Tensor(rng.uniform(lower, upper, size=shape))
