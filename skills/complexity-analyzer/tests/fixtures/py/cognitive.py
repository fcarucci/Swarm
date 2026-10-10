def flat(a):
    if a:
        return 1
    return 0


def nested(xs, y):
    total = 0
    for x in xs:
        if x > y:
            if x and y:
                total += 1
            else:
                total -= 1
    return total


class Box:
    def __init__(self, a, b):
        self.a = a
        self.b = b

    def get(self):
        return self.a
