"""Explicit synthetic deployment; never a production integration fallback."""
import os
os.environ['APP_ENV'] = 'isolated'
from .settings_staging import *
if os.getenv('ISOLATED_TEST') != 'true' or not DATABASES['default']['NAME'].startswith(('test_', 'smartcare_sandbox', 'restore_')):
    raise ImproperlyConfigured('Isolated providers require an explicitly disposable database.')
PAYMENT_PROVIDER = 'isolated_http'
SMS_PROVIDER = 'isolated_http'
ISOLATED_PROVIDER_URL = os.environ['ISOLATED_PROVIDER_URL']
PAYMENT_CALLBACK_SECRET = os.environ['PAYMENT_CALLBACK_SECRET']
EMAIL_BACKEND = 'accounts.email_backend.DatabaseEmailBackend'

ISOLATED_TEST_ENABLED = True
