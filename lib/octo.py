import hashlib, json, sys, urllib.parse, urllib.request
from functools import lru_cache
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import urllib3
from google.protobuf.json_format import MessageToDict, ParseDict
from lib.proto import octodb_pb2 as octop

urllib3.disable_warnings()

_HTTP_OPTIONS = {"maxsize": 32, "cert_reqs": "CERT_NONE", "assert_hostname": False}
_DIRECT_HTTP = urllib3.PoolManager(**_HTTP_OPTIONS)


@lru_cache(maxsize=4)
def _proxy_http(proxy_url: str):
    return urllib3.ProxyManager(proxy_url, **_HTTP_OPTIONS)


def _http_request(method: str, url: str, **kwargs):
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname and not urllib.request.proxy_bypass(parsed.hostname):
        proxy_url = urllib.request.getproxies().get(parsed.scheme)
        if proxy_url:
            return _proxy_http(proxy_url).request(method, url, **kwargs)
    return _DIRECT_HTTP.request(method, url, **kwargs)


class OctoClient:
    def __init__(self, octo_cfg: dict):
        self.app_id = octo_cfg.get("app_id", 400)
        self.client_secret = octo_cfg.get("client_secret", "")
        self.version = octo_cfg.get("version", "")
        self.base_url = octo_cfg.get("url", "")
        self.api_key_seed = octo_cfg.get("api_key_seed", "")
        self.cache_key = bytes.fromhex(octo_cfg.get("cache_key", ""))
        self.cache_iv = bytes.fromhex(octo_cfg.get("cache_iv", ""))
        self.data_path = Path(octo_cfg.get("data_path", "cache/octo"))
        self.data_path.mkdir(parents=True, exist_ok=True)
        self._local_db_path = self.data_path / "OctoManifest.json"

    def fetch_database(self, revision: int = 0) -> octop.Database:
        url = urllib.parse.urljoin(
            self.base_url,
            f"v2/pub/a/{self.app_id}/v/{self.version}/list/{revision}",
        )
        headers = {
            "Accept": f"application/x-protobuf,x-octo-app/{self.app_id}",
            "X-OCTO-KEY": self.client_secret,
        }
        r = _http_request("GET", url, headers=headers)
        key = hashlib.sha256(self.api_key_seed.encode()).digest()
        cipher = Cipher(algorithms.AES(key), modes.CBC(r.data[:16])).decryptor()
        pt = cipher.update(r.data[16:]) + cipher.finalize()
        pt = pt[:-pt[-1]]
        return octop.Database.FromString(pt)

    def load_octo_cache(self, cache_path: Path) -> octop.Database | None:
        if not cache_path.exists():
            return None
        data = cache_path.read_bytes()
        cipher = Cipher(algorithms.AES(self.cache_key), modes.CBC(self.cache_iv)).decryptor()
        pt = cipher.update(data[1:]) + cipher.finalize()
        pad_len = pt[-1]
        pt = pt[:-pad_len]
        return octop.Database.FromString(pt[16:])

    def load_local_db(self) -> octop.Database | None:
        if not self._local_db_path.exists():
            return None
        with open(self._local_db_path, "r", encoding="utf8") as f:
            return ParseDict(json.load(f), octop.Database(), ignore_unknown_fields=True)

    def save_local_db(self, db: octop.Database):
        with open(self._local_db_path, "w", encoding="utf8") as f:
            json.dump(
                MessageToDict(db, use_integers_for_enums=True, always_print_fields_with_no_presence=True),
                f,
                ensure_ascii=False,
                indent=2,
            )

    def download_adv_txts(self, db: octop.Database, dest_dir: Path, workers: int = 32) -> int:
        url_fmt = db.urlFormat
        adv_txts = [r for r in db.resourceList if r.name.startswith("adv_") and r.name.endswith(".txt")]
        dest_dir.mkdir(parents=True, exist_ok=True)

        from concurrent.futures import ThreadPoolExecutor, as_completed

        headers = {"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; GM1910 Build/RKQ1.201022.002)"}
        log_path = dest_dir.parent / "download_log.json"
        log = {}
        if log_path.exists():
            log = json.loads(log_path.read_text(encoding="utf-8"))

        def dl_one(r):
            if r.name in log:
                return (r.name, "cached")
            url = url_fmt.replace("{o}", r.objectName)
            for attempt in range(3):
                try:
                    resp = _http_request("GET", url, headers=headers, timeout=30)
                    (dest_dir / r.name).write_bytes(resp.data)
                    resp.release_conn()
                    return (r.name, "ok")
                except Exception as e:
                    if attempt == 2:
                        return (r.name, str(e)[:60])

        remaining = [r for r in adv_txts if r.name not in log]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(dl_one, r): r.name for r in remaining}
            for f in as_completed(futs):
                name, status = f.result()
                log[name] = status

        log_path.write_text(json.dumps(log), encoding="utf-8")
        return len(adv_txts)
