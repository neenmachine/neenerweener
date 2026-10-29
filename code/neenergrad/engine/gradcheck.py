import numpy as np
from typing import List
from collections.abc import Callable
from engine.value import Value
from engine.tensor import Tensor

class Gradcheck:
    def __init__(self):
        pass

    @classmethod
    def scalar(cls, f: Callable[..., Value], params: List[Value]):
        h = 1e-5
        out = f(*params)
        out.backwards()

        grads = {}
        numeric_grads = {}
        for param in params:
            grads[param] = param.grad

        for idx in range(len(params)):
            value = params[idx]
            prev = value.data
            p0, p1 = prev+h, prev-h

            params[idx] = Value(p0)
            o0 = f(*params).data
            params[idx] = Value(p1)
            o1 = f(*params).data
            params[idx] = value

            numeric = (o0 - o1) / (2*h)
            numeric_grads[value] = numeric

        for p in params:
            grad, numeric = grads[p], numeric_grads[p]
            check = abs(grad - numeric) / max(1, max(abs(grad), abs(numeric)))

            if check >= 1e-6:
                return False
        return True

    @classmethod
    def tensor(cls, f: Callable[..., Tensor], params: List[Tensor]):
        h = 1e-5
        out = f(*params)
        out.backwards()

        grads = {}
        numeric_grads = {}

        for param in params:
            grads[param] = param.grad

        for idx in range(len(params)):
            tensor = params[idx]
            data = tensor.data
            num_grads = np.zeros(data.shape)

            for i,x in enumerate(data.flat):
                value = data.flat[i]
                data.flat[i] = value + h
                params[idx] = Tensor(data)
                o0 = f(*params).data

                data.flat[i] = value - h
                params[idx] = Tensor(data)
                o1 = f(*params).data

                data.flat[i] = value
                numeric = (o0 - o1) / (2*h)
                num_grads.flat[i] = numeric
            params[idx] = tensor
            numeric_grads[tensor] = num_grads

        for p in params:
            grad, numeric = grads[p], numeric_grads[p]


            check = np.abs(grad-numeric) / np.maximum(1, np.maximum(np.abs(grad), np.abs(numeric)))

            if np.all(check <= 1e-6):
                continue
            return False
        return True
