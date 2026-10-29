# backprop

notes taken while reading [christopher olah's 2015 blog](https://colah.github.io/posts/2015-08-Backprop/)

## computational graphs
consider the expression $e = (a+b) \cdot (b+1)$

in total, there are 3 ops: $(a+b)$, $(b+1)$ and the product of these 2 intermediate results

let: $c = (a + b)$ and $d = (b+1)$, we now have $e = c \cdot d$

```mermaid
flowchart LR
A(["a"])
B(["b"])
A --> C["c=a+b"]
B --> C
B --> D["d=b+1"]
C --> E["e=c*d"]
D --> E
```

## partial derivatives
lets now compute the derivatives of the graph going from left to right. since a and b are both scalars, computing partial derivatives should be relatively simple.

$$
\dfrac{\partial c}{\partial a} = \dfrac{\partial (a+b)}{\partial a} = 1
$$

similarly: 

$$\dfrac{\partial c}{\partial b} = 1$$ 

$$\dfrac{\partial d}{\partial b} = 1$$

computing the partial derivatives from the second to third layer is a similar process.

$$
\dfrac{\partial e}{\partial c} = \dfrac{\partial (c \cdot d)}{\partial c} = d
$$

similarly: 

$$\dfrac{\partial e}{\partial d} = c$$

### chain rule
lets now think about how $a$ and $b$ affect $e$

$$
\dfrac{\partial e}{\partial a} = \dfrac{\partial (c \cdot d)}{\partial a}
$$

$$
\implies \dfrac{\partial e}{\partial a} = \dfrac{\partial [c(a) \cdot d(a)]}{\partial a} 
$$

$$
\implies \dfrac{\partial e}{\partial a} = \dfrac{\partial c(a)}{\partial a} \cdot d(a) + \dfrac{\partial d(a)}{\partial a} \cdot c(a)
$$

$$
\implies \dfrac{\partial e}{\partial a} = 1 \cdot d(a) + 0 \cdot c(a) = d(a) = b+1
$$

you can also think about this in terms of multivariate chain rule:

$$
\dfrac{\partial e}{\partial a} = \dfrac{\partial e}{\partial c} \cdot \dfrac{\partial c}{\partial a} + \dfrac{\partial e}{\partial d} \cdot \dfrac{\partial d}{\partial a}
$$

$$
= (d) \cdot (1) + (c) \cdot (0)
$$

$$
= b + 1
$$

similarly:

$$
\dfrac{\partial e}{\partial b} = \dfrac{\partial e}{\partial c} \cdot \dfrac{\partial c}{\partial b} + \dfrac{\partial e}{\partial d} \cdot \dfrac{\partial d}{\partial b}
$$

$$
= (d) \cdot (1) + (c) \cdot (1)
$$

$$
= d + c
$$

$$
= a + 2b + 1
$$

visually, this can be derived by summing up the product of all the paths from $b \rightarrow e$

## forward mode differentiation
