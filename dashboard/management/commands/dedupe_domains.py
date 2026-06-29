from django.core.management.base import BaseCommand

from dashboard import services


class Command(BaseCommand):
    help = "Move duplicate domain documents from the main Mongo collection to the duplicates collection."

    def handle(self, *args, **options):
        client, col = services.collection()
        try:
            before_docs = col.count_documents({})
            before_unique = len(col.distinct("domain"))
            duplicate_col = client[services.MONGO_DB][services.MONGO_DUPLICATE_COLLECTION]
            before_archived = duplicate_col.count_documents({})

            moved = services.move_duplicate_domains(client, col)

            after_docs = col.count_documents({})
            after_unique = len(col.distinct("domain"))
            after_archived = duplicate_col.count_documents({})
            remaining_groups = list(
                col.aggregate(
                    [
                        {"$group": {"_id": "$domain", "count": {"$sum": 1}}},
                        {"$match": {"count": {"$gt": 1}}},
                        {"$limit": 1},
                    ]
                )
            )

            self.stdout.write(f"Main collection: {services.MONGO_COLLECTION}")
            self.stdout.write(f"Duplicate collection: {services.MONGO_DUPLICATE_COLLECTION}")
            self.stdout.write(f"Before: docs={before_docs}, unique_domains={before_unique}, archived={before_archived}")
            self.stdout.write(f"Moved now: {moved}")
            self.stdout.write(f"After: docs={after_docs}, unique_domains={after_unique}, archived={after_archived}")
            self.stdout.write(f"Remaining duplicate groups: {len(remaining_groups)}")
        finally:
            client.close()
