def parameter(name: str, location: str, schema_type: str, description: str, required: bool = False) -> dict:
    return {
        "name": name,
        "in": location,
        "required": required,
        "description": description,
        "schema": {"type": schema_type},
    }


AUTH_RESPONSES = {
    "401": {"description": "Invalid or missing API key"},
    "500": {"description": "API key is not configured"},
}

JSON_RESPONSE = {
    "200": {
        "description": "Successful JSON response",
        "content": {"application/json": {"schema": {"type": "object"}}},
    },
    **AUTH_RESPONSES,
}


def openapi_schema() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Expired Domains Dashboard API",
            "version": "1.0.0",
            "description": "Django API for controlling the scraper and browsing MongoDB domain data.",
        },
        "servers": [{"url": "/", "description": "Current Django server"}],
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "200m-API-Key",
                },
                "XApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                }
            }
        },
        "security": [{"ApiKeyAuth": []}, {"XApiKeyAuth": []}],
        "paths": {
            "/api/docs/": {
                "get": {
                    "tags": ["System"],
                    "summary": "Render Swagger API documentation",
                    "description": "Requires dashboard session login.",
                    "security": [],
                    "responses": {"200": {"description": "Swagger documentation page"}},
                }
            },
            "/api/schema/": {
                "get": {
                    "tags": ["System"],
                    "summary": "Return this OpenAPI schema",
                    "description": "Requires dashboard session login.",
                    "security": [],
                    "responses": {"200": {"description": "OpenAPI schema JSON"}},
                }
            },
            "/api/health/": {
                "get": {
                    "tags": ["System"],
                    "summary": "Health and database status",
                    "security": [],
                    "responses": {"200": {"description": "Health response"}},
                }
            },
            "/api/main/start/": {
                "post": {"tags": ["Scraper"], "summary": "Start scraper process", "responses": JSON_RESPONSE}
            },
            "/api/main/stop/": {
                "post": {"tags": ["Scraper"], "summary": "Stop scraper process", "responses": JSON_RESPONSE}
            },
            "/api/main/restart/": {
                "post": {"tags": ["Scraper"], "summary": "Restart scraper process", "responses": JSON_RESPONSE}
            },
            "/api/main/status/": {
                "get": {"tags": ["Scraper"], "summary": "Get compact scraper status", "responses": JSON_RESPONSE}
            },
            "/api/main/logs/": {
                "get": {
                    "tags": ["Scraper"],
                    "summary": "Get scraper status and recent logs",
                    "parameters": [parameter("lines", "query", "integer", "Number of log lines, 1-1000")],
                    "responses": JSON_RESPONSE,
                }
            },
            "/api/database/backup/": {
                "get": {
                    "tags": ["Database"],
                    "summary": "Download a gzip MongoDB backup",
                    "responses": {
                        "200": {
                            "description": "Gzip backup file",
                            "content": {"application/gzip": {"schema": {"type": "string", "format": "binary"}}},
                        },
                        **AUTH_RESPONSES,
                    },
                }
            },
            "/api/database/restore/": {
                "post": {
                    "tags": ["Database"],
                    "summary": "Restore MongoDB from a dashboard gzip backup",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"backup": {"type": "string", "format": "binary"}},
                                    "required": ["backup"],
                                }
                            }
                        },
                    },
                    "responses": {**JSON_RESPONSE, "400": {"description": "Invalid or missing gzip backup"}},
                }
            },
            "/api/telegram/settings/": {
                "get": {"tags": ["Telegram"], "summary": "Read Telegram notification settings", "responses": JSON_RESPONSE},
                "post": {"tags": ["Telegram"], "summary": "Save Telegram notification settings", "responses": JSON_RESPONSE},
            },
            "/api/telegram/test/": {
                "post": {
                    "tags": ["Telegram"],
                    "summary": "Send a Telegram test notification",
                    "responses": {**JSON_RESPONSE, "400": {"description": "Telegram configuration or send failed"}},
                }
            },
            "/api/scraper/settings/": {
                "get": {"tags": ["Scraper"], "summary": "Read editable scraper settings", "responses": JSON_RESPONSE}
            },
            "/api/scraper/interval/": {
                "post": {
                    "tags": ["Scraper"],
                    "summary": "Update ExpiredDomains scrape interval in seconds",
                    "parameters": [
                        parameter("seconds", "query", "integer", "Interval seconds, minimum 60"),
                        parameter("X-Scrape-Interval-Seconds", "header", "integer", "Interval seconds, minimum 60"),
                    ],
                    "responses": JSON_RESPONSE,
                }
            },
            "/api/scraper/start-page-1/": {
                "post": {
                    "tags": ["Scraper"],
                    "summary": "Reset the scraper page cursor and start from page 1",
                    "responses": JSON_RESPONSE,
                }
            },
            "/api/domains/": {
                "get": {
                    "tags": ["Domains"],
                    "summary": "List unique MongoDB domains",
                    "parameters": [
                        parameter("limit", "query", "integer", "Rows to return, 1-1000"),
                        parameter("skip", "query", "integer", "Rows to skip for pagination"),
                        parameter("batch", "query", "integer", "Filter by batch number"),
                        parameter("date_from", "query", "string", "Filter exported_at from date, YYYY-MM-DD"),
                        parameter("date_to", "query", "string", "Filter exported_at to date, YYYY-MM-DD"),
                    ],
                    "responses": JSON_RESPONSE,
                }
            },
            "/api/domains/{domain}/": {
                "get": {
                    "tags": ["Domains"],
                    "summary": "Get one domain record",
                    "parameters": [parameter("domain", "path", "string", "Domain name", True)],
                    "responses": {
                        **JSON_RESPONSE,
                        "404": {"description": "Domain not found"},
                    },
                }
            },
            "/api/duplicates/": {
                "get": {
                    "tags": ["Domains"],
                    "summary": "List archived duplicate domain records",
                    "parameters": [
                        parameter("limit", "query", "integer", "Rows to return, 1-1000"),
                        parameter("skip", "query", "integer", "Rows to skip for pagination"),
                        parameter("domain", "query", "string", "Search duplicate records by domain"),
                    ],
                    "responses": JSON_RESPONSE,
                }
            },
            "/api/batches/": {
                "get": {"tags": ["Domains"], "summary": "List domain batches", "responses": JSON_RESPONSE}
            },
            "/api/seo/check/": {
                "post": {
                    "tags": ["SEO"],
                    "summary": "Start an SEO check job",
                    "parameters": [
                        parameter("limit", "query", "integer", "Maximum domains to check, 1-10000"),
                        parameter("force", "query", "boolean", "Force recheck"),
                        parameter("recheck_days", "query", "integer", "Recheck interval in days"),
                    ],
                    "responses": JSON_RESPONSE,
                }
            },
            "/api/seo/jobs/{job_id}/": {
                "get": {
                    "tags": ["SEO"],
                    "summary": "Get SEO job status",
                    "parameters": [parameter("job_id", "path", "string", "SEO job id", True)],
                    "responses": {
                        **JSON_RESPONSE,
                        "404": {"description": "SEO job not found"},
                    },
                }
            },
            "/api/seo/checks/": {
                "get": {
                    "tags": ["SEO"],
                    "summary": "List SEO check records",
                    "parameters": [
                        parameter("limit", "query", "integer", "Rows to return, 1-1000"),
                        parameter("skip", "query", "integer", "Rows to skip for pagination"),
                        parameter("reachable", "query", "boolean", "Filter by reachability"),
                        parameter("domain", "query", "string", "Search by domain"),
                        parameter("min_score", "query", "integer", "Minimum SEO score"),
                        parameter("max_score", "query", "integer", "Maximum SEO score"),
                    ],
                    "responses": JSON_RESPONSE,
                }
            },
            "/api/seo/rankings/": {
                "get": {
                    "tags": ["SEO"],
                    "summary": "List SEO rankings",
                    "parameters": [
                        parameter("limit", "query", "integer", "Rows to return, 1-1000"),
                        parameter("skip", "query", "integer", "Rows to skip for pagination"),
                        parameter("reachable", "query", "boolean", "Filter by reachability"),
                    ],
                    "responses": JSON_RESPONSE,
                }
            },
            "/api/seo/sheet/": {
                "get": {
                    "tags": ["SEO"],
                    "summary": "Get SEO sheet as JSON or CSV",
                    "parameters": [
                        parameter("limit", "query", "integer", "Rows to return, 1-5000"),
                        parameter("skip", "query", "integer", "Rows to skip for pagination"),
                        parameter("reachable", "query", "boolean", "Filter by reachability"),
                        parameter("domain", "query", "string", "Search by domain"),
                        parameter("min_score", "query", "integer", "Minimum SEO score"),
                        parameter("format", "query", "string", "json or csv"),
                    ],
                    "responses": JSON_RESPONSE,
                }
            },
            "/api/wsc/check/": {
                "post": {
                    "tags": ["Website SEO Checker"],
                    "summary": "Submit up to 50 unchecked unique domains to Website SEO Checker",
                    "parameters": [
                        parameter("limit", "query", "integer", "Domains to submit, max 50"),
                        parameter("force", "query", "boolean", "Recheck domains already saved in WSC collection"),
                    ],
                    "responses": JSON_RESPONSE,
                }
            },
            "/api/wsc/jobs/{job_id}/": {
                "get": {
                    "tags": ["Website SEO Checker"],
                    "summary": "Get Website SEO Checker job status",
                    "parameters": [parameter("job_id", "path", "string", "WSC job id", True)],
                    "responses": {
                        **JSON_RESPONSE,
                        "404": {"description": "Website SEO Checker job not found"},
                    },
                }
            },
            "/api/wsc/results/": {
                "get": {
                    "tags": ["Website SEO Checker"],
                    "summary": "List Website SEO Checker result rows saved in MongoDB",
                    "parameters": [
                        parameter("limit", "query", "integer", "Rows to return, 1-1000"),
                        parameter("skip", "query", "integer", "Rows to skip for pagination"),
                        parameter("domain", "query", "string", "Search by domain"),
                        parameter("da_min", "query", "integer", "Minimum DA"),
                        parameter("da_max", "query", "integer", "Maximum DA"),
                        parameter("pa_min", "query", "integer", "Minimum PA"),
                        parameter("pa_max", "query", "integer", "Maximum PA"),
                        parameter("tbl_min", "query", "integer", "Minimum Moz Total Backlinks"),
                        parameter("tbl_max", "query", "integer", "Maximum Moz Total Backlinks"),
                        parameter("qbl_min", "query", "integer", "Minimum Moz Quality Backlinks"),
                        parameter("qbl_max", "query", "integer", "Maximum Moz Quality Backlinks"),
                        parameter("qt_min", "query", "integer", "Minimum quality backlinks percentage"),
                        parameter("qt_max", "query", "integer", "Maximum quality backlinks percentage"),
                        parameter("os_min", "query", "integer", "Minimum OS percentage"),
                        parameter("os_max", "query", "integer", "Maximum OS percentage"),
                        parameter("ss_min", "query", "integer", "Minimum SS percentage"),
                        parameter("ss_max", "query", "integer", "Maximum SS percentage"),
                        parameter("X-Domain", "header", "string", "Search by domain"),
                        parameter("X-DA-Min", "header", "integer", "Minimum DA"),
                        parameter("X-DA-Max", "header", "integer", "Maximum DA"),
                        parameter("X-PA-Min", "header", "integer", "Minimum PA"),
                        parameter("X-PA-Max", "header", "integer", "Maximum PA"),
                        parameter("X-TBL-Min", "header", "integer", "Minimum Moz Total Backlinks"),
                        parameter("X-TBL-Max", "header", "integer", "Maximum Moz Total Backlinks"),
                        parameter("X-QBL-Min", "header", "integer", "Minimum Moz Quality Backlinks"),
                        parameter("X-QBL-Max", "header", "integer", "Maximum Moz Quality Backlinks"),
                        parameter("X-QT-Min", "header", "integer", "Minimum quality backlinks percentage"),
                        parameter("X-QT-Max", "header", "integer", "Maximum quality backlinks percentage"),
                        parameter("X-OS-Min", "header", "integer", "Minimum OS percentage"),
                        parameter("X-OS-Max", "header", "integer", "Maximum OS percentage"),
                        parameter("X-SS-Min", "header", "integer", "Minimum SS percentage"),
                        parameter("X-SS-Max", "header", "integer", "Maximum SS percentage"),
                    ],
                    "responses": JSON_RESPONSE,
                }
            },
            "/api/nawala/check/": {
                "post": {
                    "tags": ["Nawala"],
                    "summary": "Run Nawala check for saved Website SEO Checker domains",
                    "parameters": [
                        parameter("limit", "query", "integer", "Domains to check, max 5000"),
                        parameter("force", "query", "boolean", "Recheck domains already saved in final Nawala collection"),
                    ],
                    "responses": JSON_RESPONSE,
                }
            },
            "/api/nawala/jobs/{job_id}/": {
                "get": {
                    "tags": ["Nawala"],
                    "summary": "Get Nawala job status",
                    "parameters": [parameter("job_id", "path", "string", "Nawala job id", True)],
                    "responses": {
                        **JSON_RESPONSE,
                        "404": {"description": "Nawala job not found"},
                    },
                }
            },
            "/api/nawala/results/": {
                "get": {
                    "tags": ["Nawala"],
                    "summary": "List final SEO plus Nawala result rows saved in MongoDB",
                    "parameters": [
                        parameter("limit", "query", "integer", "Rows to return, 1-1000"),
                        parameter("skip", "query", "integer", "Rows to skip for pagination"),
                        parameter("domain", "query", "string", "Search by domain"),
                        parameter("blocked", "query", "boolean", "Filter by Nawala blocked status"),
                        parameter("ss_min", "query", "integer", "Minimum spam score percentage"),
                        parameter("ss_max", "query", "integer", "Maximum spam score percentage"),
                    ],
                    "responses": JSON_RESPONSE,
                }
            },
            "/api/domain-lists/": {
                "get": {
                    "tags": ["Domain Lists"],
                    "summary": "List full SEO Checker rows where Nawala blocked is false",
                    "parameters": [
                        parameter("limit", "query", "integer", "Rows to return, 1-5000"),
                        parameter("skip", "query", "integer", "Rows to skip for pagination"),
                        parameter("tbl_min", "query", "integer", "Minimum Moz Total Backlinks"),
                        parameter("tbl_max", "query", "integer", "Maximum Moz Total Backlinks"),
                        parameter("qbl_min", "query", "integer", "Minimum Moz Quality Backlinks"),
                        parameter("qbl_max", "query", "integer", "Maximum Moz Quality Backlinks"),
                        parameter("qt_min", "query", "integer", "Minimum quality backlinks percentage"),
                        parameter("qt_max", "query", "integer", "Maximum quality backlinks percentage"),
                        parameter("ss_min", "query", "integer", "Minimum spam score percentage, default 1"),
                        parameter("ss_max", "query", "integer", "Maximum spam score percentage, default 1"),
                        parameter("last_hours", "query", "integer", "Filter nawala_checked_at to the last N hours. Default 24. Use 0 to disable this and use date/time filters."),
                        parameter("date_from", "query", "string", "Filter nawala_checked_at from date, YYYY-MM-DD"),
                        parameter("date_to", "query", "string", "Filter nawala_checked_at to date, YYYY-MM-DD"),
                        parameter("time_from", "query", "string", "Filter nawala_checked_at from time, HH:MM or HH:MM:SS"),
                        parameter("time_to", "query", "string", "Filter nawala_checked_at to time, HH:MM or HH:MM:SS"),
                        parameter("checked_from", "query", "string", "Filter nawala_checked_at from ISO datetime"),
                        parameter("checked_to", "query", "string", "Filter nawala_checked_at to ISO datetime"),
                        parameter("X-TBL-Min", "header", "integer", "Minimum Moz Total Backlinks"),
                        parameter("X-TBL-Max", "header", "integer", "Maximum Moz Total Backlinks"),
                        parameter("X-QBL-Min", "header", "integer", "Minimum Moz Quality Backlinks"),
                        parameter("X-QBL-Max", "header", "integer", "Maximum Moz Quality Backlinks"),
                        parameter("X-QT-Min", "header", "integer", "Minimum quality backlinks percentage"),
                        parameter("X-QT-Max", "header", "integer", "Maximum quality backlinks percentage"),
                        parameter("X-SS-Min", "header", "integer", "Minimum spam score percentage, default 1"),
                        parameter("X-SS-Max", "header", "integer", "Maximum spam score percentage, default 1"),
                        parameter("X-Last-Hours", "header", "integer", "Filter nawala_checked_at to the last N hours. Default 24. Use 0 to disable this and use date/time headers."),
                        parameter("X-Date-From", "header", "string", "Filter nawala_checked_at from date, YYYY-MM-DD"),
                        parameter("X-Date-To", "header", "string", "Filter nawala_checked_at to date, YYYY-MM-DD"),
                        parameter("X-Time-From", "header", "string", "Filter nawala_checked_at from time, HH:MM or HH:MM:SS"),
                        parameter("X-Time-To", "header", "string", "Filter nawala_checked_at to time, HH:MM or HH:MM:SS"),
                        parameter("X-Checked-From", "header", "string", "Filter nawala_checked_at from ISO datetime"),
                        parameter("X-Checked-To", "header", "string", "Filter nawala_checked_at to ISO datetime"),
                    ],
                    "responses": JSON_RESPONSE,
                }
            },
        },
    }
