def connect(host, port, user, timeout):
    return host, port, user, timeout


def reconnect(host, port, user, retries):
    return host, port, user, retries


def probe(host, port, user):
    return host, port, user


def unrelated(a, b):
    return a
