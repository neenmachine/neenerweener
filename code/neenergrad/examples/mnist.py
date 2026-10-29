import numpy as np
import math

from engine.mlp import MLP
from engine.optim import SGD, Momentum
from engine.tensor import Tensor

rng = np.random.default_rng(0)

def parse(f: str) -> np.ndarray:
    with open(f, "rb") as file:
        raw = file.read()

    magic = np.frombuffer(raw, dtype='>i4', count=1)[0]
    ndims = magic & 0xFF
    shape = tuple(np.frombuffer(raw, dtype='>i4', count=ndims, offset=4))
    data = np.frombuffer(raw, dtype=np.uint8, offset=4+4*ndims)

    return data.reshape(shape)

def random(shape: tuple[int,...]) -> Tensor:
    scale = 1/math.sqrt(shape[0])
    data = rng.random(shape) * 2 * scale - scale
    return Tensor(data)

def accuracy(mnist: MLP, X_test: Tensor, Y_test: List[int]):
    outputs = mnist(X_test).data
    correct = 0

    for idx, output in enumerate(outputs):
        guess = list(output).index(max(output))

        if guess == Y_test[idx]:
            correct += 1

    return float(correct) / len(Y_test)

def main():
    X_train, Y_train = parse('downloads/train-images.idx3-ubyte'), parse('downloads/train-labels.idx1-ubyte')
    X_test, Y_test = parse('downloads/t10k-images.idx3-ubyte'), parse('downloads/t10k-labels.idx1-ubyte')

    X_train = X_train.reshape(X_train.shape[0], -1) / 255
    X_test = X_test.reshape(X_test.shape[0], -1) / 255

    mlp = MLP(
                dimensions = [784, 512, 256, 10],
                nonlinearity = [Tensor.relu, Tensor.relu, None],
                f = random
             )

    max_acc = 0
    training_size = 60000
    epochs = 20
    lr = 0.1
    batch_size = 64
    #optim = SGD(lr=lr, params=mlp.parameters()) 
    optim = Momentum(lr=lr, params=mlp.parameters())

    for step in range(epochs):
        perms = rng.permutation(len(X_train))
        X_train = X_train[perms]
        Y_train = Y_train[perms]
        for k in range(training_size // batch_size + 1):
            start, end = k * batch_size, min((k+1) * (batch_size), training_size)
            X_batch, Y_batch = Tensor(X_train[start:end]), Y_train[start:end]

            output = mlp(X_batch)
            entropy = output.cross_entropy(Y_batch)
            entropy.backwards()

            optim.step()
            optim.zero_grad()
        acc = accuracy(mlp, Tensor(X_test), Y_test)
        print(f"epoch [{step}] loss={entropy.data}, accuracy={acc * 100}%")
        max_acc = max(max_acc, acc)
    print(f"max accuracy: {max_acc}")
    
if __name__ == "__main__":
    main()
