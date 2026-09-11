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
from .phase6 import (import_gateway_csv, create_payables, doctor_statement,
                     create_settlement, change_settlement, export_settlement, operations_report,
                     add_refund_adjustment)


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


class GatewayImportInput(StrictInput):
    facility_id = serializers.IntegerField(min_value=1)


class FacilityInput(StrictInput):
    facility_id = serializers.IntegerField(min_value=1)


class SettlementInput(FacilityInput):
    reference = serializers.CharField(max_length=128)
    adjustment = serializers.DecimalField(max_digits=12, decimal_places=2, default=0)


class RefundAdjustmentInput(StrictInput):
    paid_batch_id = serializers.IntegerField(min_value=1)
    payable_id = serializers.IntegerField(min_value=1)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)


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


class GatewayImportView(DomainView):
    def post(self, request):
        form = GatewayImportInput(data=request.data)
        form.is_valid(raise_exception=True)
        upload = request.FILES.get('file')
        if not upload:
            return Response({'code': 'validation', 'message': 'CSV file is required.'}, status=400)
        return Response(import_gateway_csv(request.user, form.validated_data['facility_id'], upload), status=201)


class PayablesView(DomainView):
    def post(self, request):
        form = FacilityInput(data=request.data)
        form.is_valid(raise_exception=True)
        return Response({'results': create_payables(request.user, form.validated_data['facility_id'])}, status=201)


class DoctorStatementView(DomainView):
    def get(self, request):
        return Response({'results': doctor_statement(request.user)})


class SettlementView(DomainView):
    def post(self, request):
        form = SettlementInput(data=request.data)
        form.is_valid(raise_exception=True)
        return Response(create_settlement(request.user, **form.validated_data), status=201)


class SettlementActionView(DomainView):
    def post(self, request, pk, action):
        return Response(change_settlement(request.user, pk, action))


class SettlementExportView(DomainView):
    def get(self, request, pk):
        return export_settlement(request.user, pk)


class RefundAdjustmentView(DomainView):
    def post(self, request):
        form = RefundAdjustmentInput(data=request.data)
        form.is_valid(raise_exception=True)
        return Response(add_refund_adjustment(request.user, **form.validated_data), status=201)


class OperationsReportView(DomainView):
    def get(self, request):
        form = FacilityInput(data=request.query_params)
        form.is_valid(raise_exception=True)
        return Response(operations_report(request.user, form.validated_data['facility_id']))
