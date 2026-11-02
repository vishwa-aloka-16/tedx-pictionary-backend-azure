"""Publish finished PNGs to the independent artwork image service."""
import json
import urllib.parse
import urllib.request


class ArtworkPublisher:
    def __init__(self, base_url, upload_key):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment):
            raise ValueError("ARTWORK_SHARE_URL must be the image service's public HTTP(S) origin.")
        if len(upload_key) < 32:
            raise ValueError("ARTWORK_SHARE_UPLOAD_KEY must match the image service's private upload key.")
        self.base_url = base_url.rstrip("/")
        self.upload_key = upload_key

    def warm_up(self):
        """Wake the image service; run in a worker so gameplay never waits."""
        request = urllib.request.Request(f"{self.base_url}/health", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response).get("status") == "ok"

    def __call__(self, token, png):
        url = f"{self.base_url}/images/{urllib.parse.quote(token, safe='')}"
        request = urllib.request.Request(url, data=png, method="PUT", headers={
            "Content-Type": "image/png", "Authorization": f"Bearer {self.upload_key}",
        })
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
        # Use the configured public domain, never a localhost or unexpected URL
        # accidentally returned by an incorrectly configured image service.
        if result.get("id") != token or result.get("url") != url:
            raise ValueError("The image service's PUBLIC_BASE_URL must match ARTWORK_SHARE_URL.")
        return url
