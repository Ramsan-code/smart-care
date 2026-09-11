from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from accounts.models import DemoEmail

class Command(BaseCommand):
    help='Local operator access to simulated mail. Never available outside demo mode.'
    def add_arguments(self,parser): parser.add_argument('--recipient',required=True)
    def handle(self,*args,**options):
        if not settings.DEMO_MODE or not settings.DEBUG: raise CommandError('Only available in a local demo.')
        for mail in DemoEmail.objects.filter(recipient=options['recipient'].lower()).order_by('-created_at')[:5]:
            self.stdout.write(f'{mail.subject}\n{mail.body}\n')
