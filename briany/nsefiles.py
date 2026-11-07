"""Download NSE's daily end-of-day file. Stdlib only."""
import io
import urllib.error
import urllib.request
import zipfile

UA = {"User-Agent": "Mozilla/5.0"}
URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"


class FetchError(RuntimeError):
    """Anything other than 'file does not exist' (e.g. a 403 block or a timeout)."""


def fetch_bhavcopy(day: str, timeout: int = 30):
    """CSV text for an ISO date, or None if NSE has no file (HTTP 404 = holiday/not yet published)."""
    req = urllib.request.Request(URL.format(ymd=day.replace("-", "")), headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise FetchError(f"HTTP {e.code} for {day}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"{type(e).__name__} for {day}: {e}") from e
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        return z.read(z.namelist()[0]).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError, IndexError) as e:
        raise FetchError(f"unreadable file for {day}: {e}") from e
