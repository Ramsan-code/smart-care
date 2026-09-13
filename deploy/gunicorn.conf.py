import os
bind = 'unix:' + os.getenv('GUNICORN_SOCKET', '/run/smartcare/gunicorn.sock')
workers = int(os.getenv('WEB_WORKERS', '2'))
worker_class = 'gthread'
threads = 2
timeout = 30
graceful_timeout = 35
keepalive = 5
umask = 0o007
accesslog = None
errorlog = '-'
forwarded_allow_ips = '127.0.0.1'
secure_scheme_headers = {'X-FORWARDED-PROTO': 'https'}

no_control_socket = True
