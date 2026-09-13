import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
APP_ENV = os.getenv('APP_ENV', 'development')
if APP_ENV in ['development', 'test']:
    load_dotenv(BASE_DIR / '.env')
SECRET_KEY = os.environ['DJANGO_SECRET_KEY']
DEBUG = os.getenv('DJANGO_DEBUG', 'false').lower() == 'true'
DEMO_MODE = os.getenv('DEMO_MODE', 'false').lower() == 'true'
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')
INSTALLED_APPS = [
    'django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
    'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
    'rest_framework', 'accounts', 'configuration', 'core', 'scheduling', 'appointments', 'finance', 'communications', 'operations',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware', 'core.middleware.CorrelationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
ROOT_URLCONF = 'smartcare.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [BASE_DIR / 'templates'],
              'APP_DIRS': True, 'OPTIONS': {'context_processors': [
                  'django.template.context_processors.request', 'django.contrib.auth.context_processors.auth',
                  'django.contrib.messages.context_processors.messages', 'core.context.app_context']}}]
WSGI_APPLICATION = 'smartcare.wsgi.application'
if os.getenv('DB_ENGINE') == 'sqlite':
    DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': BASE_DIR / '.runtime' / 'smartcare.sqlite3'}}
else:
    DATABASES = {'default': {
        'ENGINE': 'django.db.backends.mysql', 'NAME': os.getenv('DB_NAME', 'smartcare'),
        'USER': os.getenv('DB_USER', 'smartcare'), 'PASSWORD': os.environ['DB_PASSWORD'],
        'HOST': os.getenv('DB_HOST', '127.0.0.1'), 'PORT': os.getenv('DB_PORT', '3307'),
        'OPTIONS': {'charset': 'utf8mb4', 'isolation_level': 'read committed',
                    'init_command': "SET sql_mode='STRICT_TRANS_TABLES'"},
        'TEST': {'NAME': 'test_smartcare'},
    }}
AUTH_USER_MODEL = 'accounts.User'
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/workspace/'
LOGOUT_REDIRECT_URL = '/'
LANGUAGE_CODE = 'en'
TIME_ZONE = 'Asia/Colombo'
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / '.runtime' / 'static'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
EMAIL_BACKEND = 'accounts.email_backend.DatabaseEmailBackend' if DEMO_MODE else 'django.core.mail.backends.smtp.EmailBackend'
DEFAULT_FROM_EMAIL = 'Smart Care <noreply@example.test>'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_SSL_REDIRECT = not DEBUG
SESSION_COOKIE_AGE = 3600
PASSWORD_RESET_TIMEOUT = 3600
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.redis.RedisCache',
                       'LOCATION': os.getenv('REDIS_CACHE_URL', 'redis://127.0.0.1:6380/1'),
                       'TIMEOUT': 60, 'KEY_PREFIX': 'smartcare'}}
CELERY_BROKER_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6380/0')
CELERY_BROKER_CONNECTION_TIMEOUT = 2
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_BEAT_SCHEDULE = {'outbox-recovery': {'task': 'core.tasks.dispatch_outbox', 'schedule': 15.0}}
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': ['accounts.authentication.SessionAuthentication401'],
    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.IsAuthenticated'],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination', 'PAGE_SIZE': 25,
    'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
}
LOGGING = {'version': 1, 'disable_existing_loggers': False,
           'handlers': {'console': {'class': 'logging.StreamHandler'}},
           'root': {'handlers': ['console'], 'level': 'INFO'}}

CELERY_BEAT_SCHEDULE['hold-expiry'] = {'task': 'appointments.tasks.expire_reservations', 'schedule': 30.0}
CELERY_BEAT_SCHEDULE['appointment-reminders'] = {'task': 'communications.tasks.queue_due_reminders', 'schedule': 60.0}

PAYMENT_PROVIDER = os.getenv('PAYMENT_PROVIDER', 'simulated')
TEST_PAYMENT_URL = os.getenv('TEST_PAYMENT_URL', 'http://127.0.0.1:8099')
REFUND_LEASE_SECONDS = int(os.getenv('REFUND_LEASE_SECONDS', '60'))
REFUND_MAX_ATTEMPTS = 8
CELERY_BROKER_TRANSPORT_OPTIONS = {'visibility_timeout': int(os.getenv('CELERY_VISIBILITY_TIMEOUT', '3600'))}
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BEAT_SCHEDULE['outbox-recovery']['schedule'] = float(os.getenv('OUTBOX_RECOVERY_SECONDS', '15'))

RESTORE_QUARANTINE = False
OPERATIONS_REDIS_URL = os.getenv('OPERATIONS_REDIS_URL', 'redis://127.0.0.1:6380/2')
HEALTH_TOKEN = os.getenv('HEALTH_TOKEN', '')
OPERATIONS_ENABLED = False
