import json
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_TTL_DAYS = 30


class CacheService:
    def __init__(self):
        self.cache_dir = DATA_DIR / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, user_email: str) -> Path:
        safe = user_email.replace("@", "_at_").replace(".", "_")
        return self.cache_dir / f"{safe}.json"

    def get(self, user_email: str) -> dict | None:
        path = self._cache_path(user_email)
        if not path.exists():
            return None

        with open(path) as f:
            data = json.load(f)

        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if datetime.now() - fetched_at > timedelta(days=CACHE_TTL_DAYS):
            return None

        return data

    def set(self, user_email: str, policies: list[dict]):
        path = self._cache_path(user_email)
        data = {
            "user_email": user_email,
            "fetched_at": datetime.now().isoformat(),
            "policies": policies,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def invalidate(self, user_email: str):
        path = self._cache_path(user_email)
        if path.exists():
            path.unlink()
