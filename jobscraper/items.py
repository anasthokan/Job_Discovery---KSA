import scrapy


class JobItem(scrapy.Item):
    id = scrapy.Field()
    title = scrapy.Field()
    company = scrapy.Field()
    skills = scrapy.Field()
    state = scrapy.Field()
    location = scrapy.Field()
    platform = scrapy.Field()
    daysAgo = scrapy.Field()
    url = scrapy.Field()
    posted_at = scrapy.Field()
    description = scrapy.Field()
    raw_description = scrapy.Field()
    job_type = scrapy.Field()
    work_model = scrapy.Field()
    experience_level = scrapy.Field()
    years_experience = scrapy.Field()
    visa_sponsorship = scrapy.Field()
    clearance_required = scrapy.Field()
    saudi_national_required = scrapy.Field()
