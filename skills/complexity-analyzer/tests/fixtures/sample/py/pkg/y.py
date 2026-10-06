from abc import ABC, abstractmethod

from pkg import x


class Base(ABC):
    @abstractmethod
    def run(self, v):
        ...


class OnlyImpl(Base):
    def run(self, v):
        return x.decide(v, True)


def helper(a, b):
    return a + b


def unused_py_helper(a, b, c):
    if a:
        for i in range(b):
            if i > c and i % 2:
                return i
    return 0
