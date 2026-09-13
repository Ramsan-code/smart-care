from django.apps import AppConfig
from django.core.checks import Error, Warning, register


class FinanceConfig(AppConfig):
    name = 'finance'

    def ready(self):
        from django.conf import settings

        @register()
        def payment_configuration(app_configs, **kwargs):
            if settings.PAYMENT_PROVIDER == 'isolated_http':
                from operations.providers import require_isolation
                try:
                    require_isolation()
                except Exception:
                    return [Error('Invalid isolated integration profile.', id='finance.E003')]
                return []
            if settings.PAYMENT_PROVIDER == 'disabled' and settings.APP_ENV in ['staging', 'production']:
                return [Warning('Financial integrations disabled; release readiness is blocked.', id='finance.W001')]
            if not (settings.DEBUG and settings.DEMO_MODE):
                return [Error('No production payment provider is implemented. Demo providers require DEBUG and DEMO_MODE.',
                              id='finance.E001')]
            if settings.PAYMENT_PROVIDER not in ['simulated', 'test_http']:
                return [Error('Unknown payment provider.', id='finance.E002')]
            return []
