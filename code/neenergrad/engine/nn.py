from engine.value import Value
from typing import List
from collections.abc import Callable
import random

class Neuron:
    def __init__(self, 
                 wlength: int,
                 nonlinearity: Callable[[Value], Value] = None,
                 lower: float = -1.0,
                 upper: float = +1.0):
        self.weights = []
        self.bias = Value(0)
        self.nonlinearity = nonlinearity

        for x in range(wlength):
            self.weights.append(Value(random.uniform(lower, upper)))

    def __call__(self, inputs: List[Value]) -> Value: 
        assert(len(inputs) == len(self.weights))

        L = len(inputs)
        result = Value(0)
        for idx in range(L):
            result += self.weights[idx] * inputs[idx]
        result += self.bias

        return result if self.nonlinearity is None else self.nonlinearity(result)

    def parameters(self) -> List[Value]:
        return self.weights + [self.bias]

class Layer:
    def __init__(self,
                 n_inputs: int,
                 n_outputs: int,
                 lower: float = -1.0,
                 upper: float = +1.0,
                 nonlinearity: Callable[[Value], Value] = None):
        self.neurons = []

        for x in range(n_outputs):
            nn = Neuron(n_inputs, nonlinearity, lower, upper)
            self.neurons.append(nn)

    def __call__(self, inputs: List[Value]) -> List[Value]:
        outputs = []
        for neuron in self.neurons:
            result = neuron(inputs)
            outputs.append(result)
        return outputs

    def parameters(self) -> List[Value]:
        ll = []

        for neuron in self.neurons:
            ll.extend(neuron.parameters())
        return ll

class MLP:
    # define the last dimension as the 
    # number of outputs of the MLP
    def __init__(self,
                 dimensions: List[int],
                 lower: float = -1.0,
                 upper: float = +1.0,
                 nonlins: List[Callable[[Value], Value]] = None):
        self.layers = []
        
        for x in range(0, len(dimensions) - 1):
            d0, d1 = dimensions[x], dimensions[x+1]
            f = nonlins[x] if nonlins is not None and x < len(nonlins) else None
            ll = Layer(d0, d1, lower, upper, f)
            self.layers.append(ll)

    def __call__(self, inputs: List[Value]) -> List[Value]:
        args = inputs
        for layer in self.layers:
            args = layer(args)
        return args

    def parameters(self) -> List[Value]:
        ll = []

        for layer in self.layers:
            ll.extend(layer.parameters())
        return ll
