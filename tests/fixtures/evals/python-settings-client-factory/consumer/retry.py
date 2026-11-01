def retry(fn, *args):
    return fn(*args)

def invoke(fn, *args):
    return retry(fn, *args)
