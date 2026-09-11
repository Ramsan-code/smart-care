from django.core.mail.backends.base import BaseEmailBackend
from .models import DemoEmail

class DatabaseEmailBackend(BaseEmailBackend):
    def send_messages(self,email_messages):
        count=0
        for message in email_messages:
            for recipient in message.to:
                DemoEmail.objects.create(recipient=recipient.strip().lower(),subject=message.subject,body=message.body)
            count+=1
        return count
