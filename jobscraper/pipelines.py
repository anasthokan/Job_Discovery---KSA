from scrapy.exceptions import DropItem


class DedupePipeline:
    def __init__(self):
        self.seen = set()

    def process_item(self, item, spider=None):
        key = item.get("url") or item.get("id")
        if not key:
            raise DropItem("Missing job url/id")
        if key in self.seen:
            raise DropItem(f"Duplicate job: {key}")
        self.seen.add(key)
        if not item.get("title") or not item.get("company"):
            raise DropItem("Missing title or company")
        return item


class MysqlPipeline:
    """Upsert this crawl's jobs into MySQL after the spider finishes."""

    def open_spider(self, spider):
        self.jobs = []
        try:
            from jobscraper.db import init_db, mysql_configured

            self.enabled = mysql_configured()
            if self.enabled:
                init_db()
        except Exception:
            spider.logger.exception("MySQL pipeline disabled")
            self.enabled = False

    def process_item(self, item, spider=None):
        if getattr(self, "enabled", False):
            self.jobs.append(dict(item))
        return item

    def close_spider(self, spider):
        if not getattr(self, "enabled", False) or not self.jobs:
            return
        try:
            from jobscraper.db import upsert_jobs

            saved = upsert_jobs(self.jobs)
            spider.logger.info("Saved %s jobs to MySQL", saved)
        except Exception:
            spider.logger.exception("MySQL upsert failed")
