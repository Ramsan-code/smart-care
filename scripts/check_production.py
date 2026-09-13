"""Verify the intentional fail-closed state until a real payment adapter is implemented."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(DJANGO_SETTINGS_MODULE='smartcare.settings', DJANGO_DEBUG='false', DEMO_MODE='false')
import django
django.setup()
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.urls import resolve, Resolver404
from finance.adapters import adapter
errors = run_checks()
assert any(e.id == 'finance.E001' for e in errors), errors
try:
    adapter()
except ImproperlyConfigured:
    pass
else:
    raise AssertionError('Production selected a simulated payment adapter.')
try:
    resolve('/api/v1/payments/checkouts/00000000-0000-0000-0000-000000000001/simulate/')
except Resolver404:
    pass
else:
    raise AssertionError('Production registered simulation route.')
print('PASS: production rejects the demo-only provider configuration and simulation route is absent.')
