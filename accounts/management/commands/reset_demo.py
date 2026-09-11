from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from accounts.models import User
from configuration.models import Organization

class Command(BaseCommand):
    help='Destructive reset of an exclusively synthetic local demo database; requires explicit confirmation.'
    def add_arguments(self,parser): parser.add_argument('--confirm',required=True)
    def handle(self,*args,**options):
        if not settings.DEMO_MODE or not settings.DEBUG or options['confirm']!='RESET-SYNTHETIC-DEMO':
            raise CommandError('Requires local debug/demo mode and --confirm RESET-SYNTHETIC-DEMO.')
        if settings.DATABASES['default']['NAME']!='smartcare': raise CommandError('Only the smartcare demo database can be reset.')
        if User.objects.exclude(email__endswith='@example.test').exists() or Organization.objects.exclude(code__in=['DEMO-ORG','OTHER-ORG']).exists():
            raise CommandError('Non-demo identities detected. Reset refused.')
        call_command('flush',interactive=False)
        credentials=settings.BASE_DIR/'demo-credentials.txt'
        if credentials.exists(): credentials.unlink()
        call_command('seed_demo')
