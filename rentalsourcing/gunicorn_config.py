"""Start background threads only after Gunicorn has forked its request workers."""
# One process owns the background consumers on the existing 512 MiB service.
# I/O threads keep the cheap readiness probe available during source callbacks.
workers=1
worker_class='gthread'
threads=4

def post_worker_init(worker):
    from rentalsourcing.dashboard import start_background
    start_background(worker.wsgi)
