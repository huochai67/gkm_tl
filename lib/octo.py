import hashlib, json, urllib.parse, urllib.request
from functools import lru_cache
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import urllib3

urllib3.disable_warnings()

OCTO_ENDPOINT = "https://api.asset.game-gakuen-idolmaster.jp/v2/pub/a/400/v/205000/list/"
OCTO_API_KEY = b"eSquJySjayO5OLLVgdTd"
X_OCTO_KEY = "0jv0wsohnnsigttbfigushbtl3a8m7l5"

# HatsuboshiToolkit keys for decrypting local octocacheevai
OCTO_CACHE_KEY = bytes.fromhex("9d8dfd7b1371612846f7ba44e01af160")
OCTO_CACHE_IV = bytes.fromhex("1c6e6f9255c0e5412712f4010225e378")

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

def _parse_varint(buf, off):
    v = s = 0
    while True:
        b = buf[off]; v |= (b & 0x7F) << s; s += 7; off += 1
        if not (b & 0x80): break
    return v, off

def _parse_proto(data, off=0, end=None):
    if end is None: end = len(data)
    fields = {}
    while off < end:
        key = data[off]; off += 1; wire = key & 0x07; num = key >> 3
        if wire == 0:
            v, off = _parse_varint(data, off)
            fields.setdefault(num, []).append(("varint", v))
        elif wire == 2:
            l, off = _parse_varint(data, off)
            v = data[off:off+l]; off += l
            fields.setdefault(num, []).append(("bytes", v))
        elif wire == 4: break
        else: break
    return fields, off

def download_octo_list() -> dict:
    r = _http_request("GET", OCTO_ENDPOINT + "0", headers={
        "User-Agent": "UnityPlayer/2022.3.21f1",
        "Accept": "application/x-protobuf,x-octo-app/400",
        "X-OCTO-KEY": X_OCTO_KEY,
        "X-Unity-Version": "2022.3.21f1",
    })
    key = hashlib.sha256(OCTO_API_KEY).digest()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(r.data[:16])).decryptor()
    pt = decryptor.update(r.data[16:]) + decryptor.finalize()
    pt = pt[:-pt[-1]]
    top, _ = _parse_proto(pt, 0)

    url_format = ""
    for t, v in top.get(5, []):
        if t == "bytes": url_format = v.decode()

    resources = []
    for t, v in top.get(4, []):
        if t == "bytes":
            f, _ = _parse_proto(v, 0)
            e = {"name": "", "objectName": ""}
            for fn, fvs in f.items():
                for ft, fv in fvs:
                    if fn == 3 and ft == "bytes": e["name"] = fv.decode(errors="replace")
                    elif fn == 11 and ft == "bytes": e["objectName"] = fv.decode(errors="replace")
            if e["name"]: resources.append(e)

    return {"revision": top.get(1, [("varint", 0)])[0][1], "urlFormat": url_format, "resources": resources}

def _parse_octo_entries(top, field_num):
    entries = []
    for t, v in top.get(field_num, []):
        if t == "bytes":
            f, _ = _parse_proto(v, 0)
            e = {"name": "", "objectName": ""}
            for fn, fvs in f.items():
                for ft, fv in fvs:
                    if fn == 3 and ft == "bytes": e["name"] = fv.decode(errors="replace")
                    elif fn == 11 and ft == "bytes": e["objectName"] = fv.decode(errors="replace")
            if e["name"]: entries.append(e)
    return entries

def load_octo_cache(cache_path: Path) -> dict | None:
    if not cache_path.exists():
        return None
    data = cache_path.read_bytes()
    cipher = Cipher(algorithms.AES(OCTO_CACHE_KEY), modes.CBC(OCTO_CACHE_IV))
    pt = cipher.decryptor().update(data[1:]) + cipher.decryptor().finalize()
    pad_len = pt[-1]
    pt = pt[:-pad_len]
    top, _ = _parse_proto(pt, 16)
    url_format = ""
    for t, v in top.get(5, []):
        if t == "bytes": url_format = v.decode()
    resources = _parse_octo_entries(top, 2) + _parse_octo_entries(top, 4)
    return {"revision": top.get(1, [("varint", 0)])[0][1], "urlFormat": url_format, "resources": resources}

def download_adv_txts(octo_list: dict, dest_dir: Path, workers: int = 32):
    url_fmt = octo_list["urlFormat"]
    adv_txts = [r for r in octo_list["resources"]
                if r["name"].startswith("adv_") and r["name"].endswith(".txt")]
    dest_dir.mkdir(parents=True, exist_ok=True)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    headers = {"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; GM1910 Build/RKQ1.201022.002)"}
    log_path = dest_dir.parent / "download_log.json"
    log = {}
    if log_path.exists(): log = json.loads(log_path.read_text(encoding="utf-8"))

    def dl_one(r):
        if r["name"] in log: return (r["name"], "cached")
        url = url_fmt.replace("{o}", r["objectName"])
        for attempt in range(3):
            try:
                resp = _http_request("GET", url, headers=headers, timeout=30)
                (dest_dir / r["name"]).write_bytes(resp.data)
                resp.release_conn()
                return (r["name"], "ok")
            except Exception as e:
                if attempt == 2:
                    return (r["name"], str(e)[:60])

    remaining = [r for r in adv_txts if r["name"] not in log]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(dl_one, r): r["name"] for r in remaining}
        for f in as_completed(futs):
            name, status = f.result()
            log[name] = status

    log_path.write_text(json.dumps(log), encoding="utf-8")
    return len(adv_txts)
