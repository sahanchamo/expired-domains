from django.core.management.base import BaseCommand

from website_seo_checker import run_next_batch


class Command(BaseCommand):
    help = "Submit up to 50 unique Mongo domains to Website SEO Checker and save results to MongoDB."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50, help="Domains to submit, max 50")
        parser.add_argument("--force", action="store_true", help="Recheck domains already in WSC result collection")

    def handle(self, *args, **options):
        result = run_next_batch(limit=options["limit"], force=options["force"])
        for key, value in result.items():
            self.stdout.write(f"{key}: {value}")
