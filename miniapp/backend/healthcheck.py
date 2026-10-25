"""Container readiness; no token, initData or filesystem path is printed."""
from urllib.request import urlopen


def main():
    try:
        with urlopen('http://127.0.0.1:8000/api/health', timeout=5) as response:
            if response.status != 200:
                raise ValueError
    except Exception:
        raise SystemExit('Mini App API is not ready') from None


if __name__ == '__main__': main()
