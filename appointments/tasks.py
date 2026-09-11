from celery import shared_task
from .services import expire_holds


@shared_task
def expire_reservations():
    return expire_holds()
