from datetime import date, timedelta
from django.core.management.base import BaseCommand, CommandError
from configuration.models import ScheduleRule
from scheduling.services import generate, BookingError


class Command(BaseCommand):
    help = 'Publish dated slots from configured working hours. Existing sessions and bookings are preserved.'
    def add_arguments(self, parser):
        parser.add_argument('--start-date', required=True, help='First facility-local date, YYYY-MM-DD')
        parser.add_argument('--days', type=int, default=30)
        parser.add_argument('--facility', type=int)
    def handle(self, *args, **options):
        try: start = date.fromisoformat(options['start_date'])
        except ValueError: raise CommandError('Use YYYY-MM-DD for --start-date.')
        if not 1 <= options['days'] <= 60: raise CommandError('Use 1–60 days.')
        rules = ScheduleRule.objects.order_by('doctor_id', 'pk')
        if options['facility']: rules = rules.filter(facility_id=options['facility'])
        count = 0
        for offset in range(options['days']):
            day = start + timedelta(days=offset)
            for rule in rules.filter(weekday=day.weekday()):
                try: _, created = generate(rule.pk, day)
                except BookingError as exc: raise CommandError(exc.message)
                count += created
        self.stdout.write(self.style.SUCCESS(f'Published {count} sessions. Existing inventory preserved.'))
