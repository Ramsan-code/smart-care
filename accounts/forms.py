from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.core.exceptions import ValidationError
from .models import User, Patient
from configuration.models import Facility


def style_form(form):
    for field in form.fields.values():
        field.widget.attrs['class']='form-check-input' if isinstance(field.widget,forms.CheckboxInput) else 'form-select' if isinstance(field.widget,forms.Select) else 'form-control'


class LoginForm(AuthenticationForm):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs); style_form(self)
        self.fields['username'].label='Email address'
        self.fields['username'].widget.attrs.update(placeholder='you@example.com',autocomplete='username')
    def clean_username(self): return self.cleaned_data['username'].strip().lower()


class RegistrationForm(UserCreationForm):
    first_name=forms.CharField(max_length=150,label='First name')
    last_name=forms.CharField(max_length=150,label='Last name')
    phone=forms.CharField(max_length=32,required=False,label='Contact number (optional)')
    consent=forms.BooleanField(label='I agree to the demo booking consent (version demo-v1).')
    facility=forms.ModelChoiceField(queryset=Facility.objects.none(),empty_label=None)
    class Meta:
        model=User
        fields=['first_name','last_name','email','phone','facility','password1','password2','consent']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['facility'].queryset=Facility.objects.filter(active=True,accepts_registration=True)
        style_form(self)
    def clean_email(self):
        email=self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email=email).exists(): raise ValidationError('This email already has an account.')
        return email


class ProfileForm(forms.ModelForm):
    class Meta:
        model=Patient
        fields=['phone','date_of_birth','assistance','sms_enabled']
        widgets={'date_of_birth':forms.DateInput(attrs={'type':'date'})}
    def __init__(self,*args,**kwargs): super().__init__(*args,**kwargs); style_form(self)


class StaffForm(forms.Form):
    name=forms.CharField(max_length=150)
    email=forms.EmailField()
    role=forms.ChoiceField(choices=[(r,label) for r,label in User.Role.choices if r!='patient'])
    facility=forms.ModelChoiceField(queryset=Facility.objects.none())
    finance_approver=forms.BooleanField(required=False,label='Can approve settlement batches (finance only)')
    def __init__(self,*args,user,**kwargs):
        from .permissions import facility_ids
        super().__init__(*args,**kwargs)
        self.fields['facility'].queryset=Facility.objects.filter(pk__in=facility_ids(user))
        style_form(self)
    def clean_email(self):
        email=self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email=email).exists(): raise ValidationError('Use a new email; existing accounts cannot be reassigned here.')
        return email
    def clean(self):
        data=super().clean()
        if data.get('finance_approver') and data.get('role')!='finance': self.add_error('finance_approver','Only finance accounts can approve settlements.')
        return data
