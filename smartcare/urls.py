from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path
from accounts import views as account_views, api
from core import views

urlpatterns=[
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
