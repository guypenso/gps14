"""Start background threads only after Gunicorn has forked its request workers."""
def post_worker_init(worker):
    from rentalsourcing.dashboard import start_background
    start_background(worker.wsgi)
