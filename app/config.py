import os
from functools import lru_cache


class Settings:
    def __init__(self):
        required = ("DATABASE_URL", "SUPABASE_URL")
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )
        self.database_url = os.environ["DATABASE_URL"]
        self.supabase_url = os.environ["SUPABASE_URL"].rstrip("/")
        self.supabase_jwt_secret = os.environ.get("SUPABASE_JWT_SECRET", "")
        self.supabase_service_role_key = os.environ.get(
            "SUPABASE_SERVICE_ROLE_KEY", ""
        )
        self.jwt_audience = os.environ.get(
            "SUPABASE_JWT_AUDIENCE", "authenticated"
        )
        self.jwt_issuer = os.environ.get(
            "SUPABASE_JWT_ISSUER", f"{self.supabase_url}/auth/v1"
        )
        self.tavily_api_key = os.environ.get("TAVILY_API_KEY", "")
        self.search_cache_ttl_seconds = int(
            os.environ.get("SEARCH_CACHE_TTL_SECONDS", "900")
        )
        self.search_request_budget = int(
            os.environ.get("SEARCH_REQUEST_BUDGET", "3")
        )
        self.allow_dev_entitlement = os.environ.get(
            "ALLOW_DEV_ENTITLEMENT", ""
        ).lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
