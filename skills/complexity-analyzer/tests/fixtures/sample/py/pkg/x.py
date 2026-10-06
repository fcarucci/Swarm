from pkg import y


def use_y(a, b):
    return y.helper(a, b)


def decide(n, flag):
    if n > 0:
        if flag:
            for i in range(n):
                if i % 2 == 0 and i > 3:
                    return "big"
        return "pos"
    return "neg"
