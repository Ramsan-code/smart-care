from rest_framework import serializers
from rest_framework.response import Response

from appointments.services import command
from scheduling.api import DomainView, StrictInput
from .services import (
    apply_signed_callback,
    assign_exception,
    cancel_appointment,
    create_checkout,
    record_change_payment,
    record_counter_payment,
    record_refund,
    request_reschedule,
    resolve_exception,
    simulate_checkout,
    payment_history,
)


class AppointmentActionInput(StrictInput):
    expected_version = serializers.IntegerField(min_value=1)


class PaymentInput(AppointmentActionInput):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)


class CheckoutInput(StrictInput):
    appointment_id = serializers.UUIDField(required=False)
    change_id = serializers.UUIDField(required=False)
    expected_version = serializers.IntegerField(min_value=1, required=False)


class ChangePaymentInput(StrictInput):
    expected_version = serializers.IntegerField(min_value=1)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)


class RescheduleInput(StrictInput):
    hold_id = serializers.UUIDField()
    expected_version = serializers.IntegerField(min_value=1)
    expected_hold_version = serializers.IntegerField(min_value=1)


class SimulateInput(StrictInput):
    outcome = serializers.ChoiceField(choices=['success', 'failure', 'delay', 'duplicate'])


class ExceptionAssignInput(StrictInput):
    expected_version = serializers.IntegerField(min_value=1)
    note = serializers.CharField(required=False, allow_blank=True, max_length=240)


class ExceptionResolveInput(StrictInput):
    expected_version = serializers.IntegerField(min_value=1)
    note = serializers.CharField(max_length=240)


class CallbackView(DomainView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        signature = request.headers.get('X-Payment-Signature', '')
        return Response(apply_signed_callback(request.body, signature))


class CheckoutView(DomainView):
    def post(self, request):
        form = CheckoutInput(data=request.data)
        form.is_valid(raise_exception=True)
        result = command(
            request.user,
            'checkout.create',
            request.headers.get('Idempotency-Key'),
            dict(request.data),
            lambda: create_checkout(request.user, **form.validated_data),
        )
        return Response(result, status=201)


class CheckoutSimulationView(DomainView):
    def post(self, request, pk):
        form = SimulateInput(data=request.data)
        form.is_valid(raise_exception=True)
        return Response(simulate_checkout(request.user, pk, form.validated_data['outcome']))


class CounterPaymentView(DomainView):
    def post(self, request, pk):
        form = PaymentInput(data=request.data)
        form.is_valid(raise_exception=True)
        result = command(request.user, 'payment.counter', request.headers.get('Idempotency-Key'),
                         {'appointment_id': str(pk), **dict(request.data)},
                         lambda: record_counter_payment(request.user, pk, **form.validated_data))
        return Response(result, status=201)


class RefundView(DomainView):
    def post(self, request, pk):
        form = PaymentInput(data=request.data)
        form.is_valid(raise_exception=True)
        result = command(request.user, 'payment.refund', request.headers.get('Idempotency-Key'),
                         {'appointment_id': str(pk), **dict(request.data)},
                         lambda: record_refund(request.user, pk, **form.validated_data))
        return Response(result, status=201)


class CancellationView(DomainView):
    def post(self, request, pk):
        form = AppointmentActionInput(data=request.data)
        form.is_valid(raise_exception=True)
        result = command(request.user, 'appointment.cancel', request.headers.get('Idempotency-Key'),
                         {'appointment_id': str(pk), **dict(request.data)},
                         lambda: cancel_appointment(request.user, pk, **form.validated_data))
        return Response(result)


class RescheduleView(DomainView):
    def post(self, request, pk):
        form = RescheduleInput(data=request.data)
        form.is_valid(raise_exception=True)
        result = command(request.user, 'appointment.reschedule', request.headers.get('Idempotency-Key'),
                         {'appointment_id': str(pk), **dict(request.data)},
                         lambda: request_reschedule(request.user, pk, **form.validated_data))
        return Response(result)


class ChangePaymentView(DomainView):
    def post(self, request, pk):
        form = ChangePaymentInput(data=request.data)
        form.is_valid(raise_exception=True)
        result = command(request.user, 'payment.change_counter', request.headers.get('Idempotency-Key'),
                         {'change_id': str(pk), **dict(request.data)},
                         lambda: record_change_payment(request.user, pk, **form.validated_data))
        return Response(result, status=201)


class PaymentHistoryView(DomainView):
    def get(self, request, pk):
        return Response({'results': payment_history(request.user, pk)})


class ExceptionAssignView(DomainView):
    def post(self, request, pk):
        form = ExceptionAssignInput(data=request.data)
        form.is_valid(raise_exception=True)
        return Response(assign_exception(request.user, pk, **form.validated_data))


class ExceptionResolveView(DomainView):
    def post(self, request, pk):
        form = ExceptionResolveInput(data=request.data)
        form.is_valid(raise_exception=True)
        return Response(resolve_exception(request.user, pk, **form.validated_data))
