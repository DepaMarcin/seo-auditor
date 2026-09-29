"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, reverse_lazy

from auditor.auth_views import ThrottledLoginView

urlpatterns = [
    path('admin/', admin.site.urls),
    # Wbudowane widoki uwierzytelniania - audyty są prywatne (patrz Audit.owner),
    # więc każdy widok aplikacji wymaga zalogowania.
    path('login/', ThrottledLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    # Zmiana własnego hasła. Reset hasła innym użytkownikom robi administrator
    # w panelu /admin/ - aplikacja nie wysyła maili, więc wariant "zapomniałem
    # hasła" nie miałby jak zadziałać.
    path(
        'change-password/',
        auth_views.PasswordChangeView.as_view(
            template_name='registration/change_password.html',
            success_url=reverse_lazy('password_change_done'),
        ),
        name='password_change',
    ),
    path(
        'change-password/done/',
        auth_views.PasswordChangeDoneView.as_view(
            template_name='registration/change_password_done.html',
        ),
        name='password_change_done',
    ),
    path('', include('auditor.urls')),
]
