class Split:
    def __init__(self):
        self.a = 0
        self.b = 0

    def read_a(self):
        return self.a

    def write_a(self, v):
        self.a = v

    def read_b(self):
        return self.b

    def pure(self, x):
        return x + 1


class Joined:
    def __init__(self):
        self.a = 0
        self.b = 0

    def get_a(self):
        return self.a

    def set_a(self, v):
        self.a = v

    def get_b(self):
        return self.b

    def total(self):
        return self.get_b() + self.a
