import hashlib
import json
from django.core.exceptions import ValidationError
from django.db import transaction
from .models import AuditEvent, OutboxEvent, IdempotencyRecord


def audit(actor, action, target, facility=None, detail=None, correlation_id=None):
    kwargs=dict(actor=actor if actor and actor.is_authenticated else None,action=action,target=str(target),facility=facility,detail=detail or {})
    if correlation_id: kwargs['correlation_id']=correlation_id
    return AuditEvent.objects.create(**kwargs)


def enqueue(key,topic,payload):
    # Caller includes this durable intent inside the business transaction.
    return OutboxEvent.objects.get_or_create(key=key,defaults={'topic':topic,'payload':payload})[0]


@transaction.atomic
def execute_once(actor,operation,key,payload,command):
    if not key or len(key)>100: raise ValidationError('A nonempty Idempotency-Key up to 100 characters is required.')
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    record,_=IdempotencyRecord.objects.get_or_create(actor=actor,operation=operation,key=key,defaults={'request_hash':digest})
    record=IdempotencyRecord.objects.select_for_update().get(pk=record.pk)
    if record.request_hash != digest: raise ValidationError('Idempotency key already used with different input.')
    if record.response is not None: return record.response
    result=command()
    record.response=result
    record.save(update_fields=['response'])
    return result
