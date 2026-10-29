from collections import defaultdict
from typing import List
import math

class Value:
    def __init__(self, data: float, _children: set[Value] = None):
        self.data = data
        self.children = set(_children) if _children else set()
        self.grad = 0
        self._backward = lambda: None

    def __mul__(self, other: Value) -> Value:
        other = other if isinstance(other, Value) else Value(other)
        out = Value(self.data * other.data, (self, other))

        def _backward():
            self.grad += out.grad * other.data
            other.grad += out.grad * self.data
        out._backward = _backward
        return out

    def __add__(self, other: Value) -> Value:
        other = other if isinstance(other, Value) else Value(other)
        out = Value(self.data + other.data, (self, other))

        def _backward():
            self.grad += out.grad
            other.grad += out.grad
        out._backward = _backward
        return out

    def __sub__(self, other: Value) -> Value:
        other = other if isinstance(other, Value) else Value(other)
        out = Value(self.data - other.data, (self, other))

        def _backward():
            self.grad += out.grad
            other.grad -= out.grad
        out._backward = _backward
        return out

    def __truediv__(self, other: Value) -> Value:
        other = other if isinstance(other, Value) else Value(other)
        out = Value(self.data / other.data, (self, other))

        def _backward():
            self.grad += out.grad / other.data
            other.grad += out.grad * (-self.data / (other.data * other.data))
        out._backward = _backward
        return out

    def __pow__(self, other: int) -> Value:
        if other == 0:
            return Value(1)

        out = Value(self.data ** other, (self,))

        def _backward():
            self.grad += out.grad * (other * (self.data ** (other-1)))
        out._backward = _backward
        return out

    def exp(self) -> Value:
        x = math.exp(self.data)
        out = Value(x, (self,))

        def _backward():
            self.grad += out.grad * x
        out._backward = _backward
        return out

    def log(self) -> Value:
        assert(self.data > 0)
        out = Value(math.log(self.data), (self,))

        def _backward():
            self.grad += out.grad / self.data
        out._backward = _backward
        return out

    def tanh(self) -> Value:
        t = math.tanh(self.data)
        out = Value(t, (self,))

        def _backward():
            self.grad += out.grad * (1-t*t)
        out._backward = _backward
        return out

    def relu(self) -> Value:
        out = Value(max(self.data, 0), (self,))

        def _backward():
            self.grad += out.grad * (1 if self.data > 0 else 0)
        out._backward = _backward
        return out

    def __radd__(self, other) -> Value:
        return self + other

    def __rsub__(self, other) -> Value:
        return Value(other) - self

    def __rmul__(self, other) -> Value:
        return self * other

    def __rtruediv__(self, other) -> Value:
        return Value(other) / self

    def __neg__(self) -> Value:
        return self.__mul__(-1)

    def __repr__(self) -> str:
        return f"Value({self.data}, grad={self.grad})"

    def topological(self) -> List[Value]:
        visited = defaultdict(bool)
        ll = []

        def dfs(node: Value):
            if visited[node]:
                return

            for child in node.children:
                dfs(child)
            ll.append(node)
            visited[node] = True
        dfs(self)
        return ll[::-1]

    def backwards(self) -> None:
        ll = self.topological()

        self.grad = 1
        for value in ll:
            value._backward()
