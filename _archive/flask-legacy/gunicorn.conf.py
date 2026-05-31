import multiprocessing
import os

bind = f"0.0.0.0:{os.getenv('PORT', '5001')}"
workers = int(os.getenv('GUNICORN_WORKERS', max(2, multiprocessing.cpu_count())))
worker_class = 'gthread'
threads = int(os.getenv('GUNICORN_THREADS', '4'))
timeout = int(os.getenv('GUNICORN_TIMEOUT', '300'))
graceful_timeout = 30
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
accesslog = '-'
errorlog = '-'
loglevel = os.getenv('GUNICORN_LOGLEVEL', 'info')
proc_name = 'impeccable'
forwarded_allow_ips = '*'
