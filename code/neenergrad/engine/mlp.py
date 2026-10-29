from collections.abc import Callable
import numpy as np
from engine.tensor import Tensor

def random_(shape: tuple[int,...]) -> Tensor:
    rng = np.random.default_rng(0)

    return Tensor(rng.random(shape))

class Layer:
    def __init__(self,
                 n_inputs: int,
                 n_outputs: int,
                 nonlinearity: Callable[[Tensor], Tensor] = None,
                 f: Callable[[tuple[int,...]], Tensor] = random_):
        self.weights = f((n_inputs, n_outputs))
        self.biases = Tensor.zeros((n_outputs,))
        self.nonlinearity = nonlinearity

    def __call__(self, x: Tensor) -> Tensor:
        result = x @ self.weights + self.biases
        return result if not self.nonlinearity else self.nonlinearity(result)

    def parameters(self) -> List[Tensor]:
        return [self.weights, self.biases]
               
class MLP:
    def __init__(self, 
                 dimensions: List[int],
                 nonlinearity: List[Callable[[Tensor], Tensor]] = None,
                 f: Callable[[tuple[int,...]], Tensor] = random_):
        self.layers = []

        for x in range(0, len(dimensions) - 1):
            inputs, outputs = dimensions[x], dimensions[x+1]
            l = nonlinearity[x] if nonlinearity and x < len(nonlinearity) else None
            self.layers.append(Layer(inputs, outputs, l, f))

    def __call__(self, x: Tensor) -> Tensor:
        tmp = x

        for ll in self.layers:
            tmp = ll(tmp)

        return tmp

    def parameters(self) -> List[Tensor]:
        params = []

        for ll in self.layers:
            params.extend(ll.parameters())

        return params

    def zero_grad(self) -> None:
        pp = self.parameters()

        for tensor in pp:
            tensor.grad = np.zeros(tensor.shape)
