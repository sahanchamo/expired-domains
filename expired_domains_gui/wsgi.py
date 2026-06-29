import os

from django.core.wsgi import get_wsgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "expired_domains_gui.settings")

application = get_wsgi_application()
