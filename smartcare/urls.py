from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path
from accounts import views as account_views, api
from core import views
from scheduling.api import AvailabilityView
from appointments.api import HoldsView, HoldDetailView, AppointmentsView, AppointmentDetailView
from finance.api import (CheckoutView, CheckoutSimulationView, CallbackView, CounterPaymentView, RefundView, CancellationView,
                         RescheduleView, ChangePaymentView, PaymentHistoryView, ExceptionAssignView, ExceptionResolveView,
                         GatewayImportView, PayablesView, DoctorStatementView, SettlementView, SettlementActionView,
                         SettlementExportView, RefundAdjustmentView, OperationsReportView)
from scheduling import views as booking_views
from communications.api import DeliveryListView, DeliveryRetryView
from appointments.operations import transition_appointment, cancel_session, resolve_cancellation
from rest_framework import serializers
from rest_framework.response import Response
from scheduling.api import DomainView, StrictInput


class OperationInput(StrictInput):
    expected_version = serializers.IntegerField(min_value=1)
    note = serializers.CharField(required=False, allow_blank=True, max_length=240)


class AppointmentOperationView(DomainView):
    def post(self, request, pk, state):
        form = OperationInput(data=request.data)
        form.is_valid(raise_exception=True)
        return Response(transition_appointment(
            request.user, pk, form.validated_data["expected_version"], state,
            form.validated_data.get("note", "")
        ))


class SessionCancelInput(StrictInput):
    expected_version = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=240)


class SessionCancelView(DomainView):
    def post(self, request, pk):
        form = SessionCancelInput(data=request.data)
        form.is_valid(raise_exception=True)
        return Response(cancel_session(request.user, pk, **form.validated_data))


class ResolutionInput(StrictInput):
    expected_version = serializers.IntegerField(min_value=1)
    status = serializers.ChoiceField(choices=["pending", "contacted", "resolved"])
    outcome = serializers.ChoiceField(
        choices=["refund", "rescheduled", "credit", "unreachable"], required=False
    )
    note = serializers.CharField(required=False, allow_blank=True, max_length=240)


class ResolutionView(DomainView):
    def post(self, request, pk):
        form = ResolutionInput(data=request.data)
        form.is_valid(raise_exception=True)
        return Response(resolve_cancellation(request.user, pk, **form.validated_data))

urlpatterns=[
    path('book/', booking_views.book, name='book'),
    path('appointments/', booking_views.history, name='appointments'),
    path('appointments/<uuid:pk>/', booking_views.detail, name='appointment-detail'),
    path('schedule/publish/', booking_views.publish, name='publish-schedule'),
    path('api/v1/availability/', AvailabilityView.as_view()),
    path('api/v1/holds/', HoldsView.as_view()),
    path('api/v1/holds/<uuid:pk>/', HoldDetailView.as_view()),
    path('api/v1/appointments/', AppointmentsView.as_view()),
    path('api/v1/appointments/<uuid:pk>/', AppointmentDetailView.as_view()),
    path('api/v1/payments/checkouts/', CheckoutView.as_view()),
    path('api/v1/payments/callback/', CallbackView.as_view()),
    path('api/v1/appointments/<uuid:pk>/payments/counter/', CounterPaymentView.as_view()),
    path('api/v1/appointments/<uuid:pk>/payments/', PaymentHistoryView.as_view()),
    path('api/v1/appointments/<uuid:pk>/refunds/', RefundView.as_view()),
    path('api/v1/appointments/<uuid:pk>/cancel/', CancellationView.as_view()),
    path('api/v1/appointments/<uuid:pk>/reschedule/', RescheduleView.as_view()),
    path('api/v1/appointment-changes/<uuid:pk>/payments/counter/', ChangePaymentView.as_view()),
    path('api/v1/finance/exceptions/<uuid:pk>/assign/', ExceptionAssignView.as_view()),
    path('api/v1/finance/exceptions/<uuid:pk>/resolve/', ExceptionResolveView.as_view()),
    path('api/v1/finance/gateway/imports/', GatewayImportView.as_view()),
    path('api/v1/finance/payables/', PayablesView.as_view()),
    path('api/v1/finance/doctor/statement/', DoctorStatementView.as_view()),
    path('api/v1/finance/settlements/', SettlementView.as_view()),
    path('api/v1/finance/settlements/<int:pk>/export/', SettlementExportView.as_view()),
    path('api/v1/finance/settlements/<int:pk>/<str:action>/', SettlementActionView.as_view()),
    path('api/v1/finance/settlements/refund-adjustments/', RefundAdjustmentView.as_view()),
    path('api/v1/finance/reports/operations/', OperationsReportView.as_view()),
    path('api/v1/communications/deliveries/', DeliveryListView.as_view()),
    path('api/v1/communications/deliveries/<int:pk>/retry/', DeliveryRetryView.as_view()),
    path('api/v1/appointments/<uuid:pk>/operations/<str:state>/', AppointmentOperationView.as_view()),
    path('api/v1/sessions/<uuid:pk>/cancel/', SessionCancelView.as_view()),
    path('api/v1/cancellation-resolutions/<int:pk>/', ResolutionView.as_view()),
    path('',views.home,name='home'),path('workspace/',views.workspace,name='workspace'),
    path('configuration/',views.configuration_overview,name='configuration'),path('audit/',views.audit_log,name='audit'),
    path('team/',account_views.staff,name='staff'),path('team/<int:pk>/toggle/',account_views.toggle_membership,name='toggle-membership'),
    path('admin/',admin.site.urls),path('accounts/login/',account_views.SignInView.as_view(),name='login'),
    path('accounts/logout/',auth_views.LogoutView.as_view(),name='logout'),
    path('accounts/register/',account_views.register,name='register'),path('accounts/verify/<str:token>/',account_views.verify_email,name='verify'),
    path('patient/profile/',account_views.profile,name='profile'),path('demo/inbox/',account_views.inbox,name='inbox'),
    path('accounts/password-reset/',auth_views.PasswordResetView.as_view(template_name='registration/password_reset_form.html',email_template_name='registration/password_reset_email.html'),name='password_reset'),
    path('accounts/password-reset/done/',auth_views.PasswordResetDoneView.as_view(template_name='registration/password_reset_done.html'),name='password_reset_done'),
    path('accounts/reset/<uidb64>/<token>/',account_views.ResetConfirmView.as_view(template_name='registration/password_reset_confirm.html'),name='password_reset_confirm'),
    path('accounts/reset/complete/',auth_views.PasswordResetCompleteView.as_view(template_name='registration/password_reset_complete.html'),name='password_reset_complete'),
    path('api/v1/me/',api.MeView.as_view()),path('api/v1/me/consents/',api.ConsentView.as_view()),
    path('api/v1/patients/',api.PatientList.as_view()),path('api/v1/patients/<uuid:pk>/',api.PatientDetail.as_view()),
    path('api/v1/catalog/',views.CatalogView.as_view()),path('api/v1/health/',views.health,name='health'),
]

if settings.DEBUG and settings.DEMO_MODE:
    urlpatterns.append(path('api/v1/payments/checkouts/<uuid:pk>/simulate/', CheckoutSimulationView.as_view()))

from operations import views as health
urlpatterns += [path('health/live/', health.live), path('health/ready/', health.ready),
                path('ops/components/', health.components), path('ops/metrics/', health.metrics)]
