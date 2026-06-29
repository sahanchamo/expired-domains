import os

from django.core.asgi import get_asgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "expired_domains_gui.settings")

application = get_asgi_application()
