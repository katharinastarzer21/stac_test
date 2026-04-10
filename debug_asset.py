import requests
import time

def test_asset_url(url: str):
    print(f"\nTesting asset URL:\n{url}\n")

    # -----------------------
    # 1. HEAD (no redirects)
    # -----------------------
    try:
        t0 = time.time()
        r = requests.head(url, allow_redirects=False, timeout=10)
        dt = time.time() - t0
        print(f"HEAD (no redirect): {r.status_code} in {dt:.2f}s")
        print(f"Headers: {r.headers}\n")
    except Exception as e:
        print(f"HEAD (no redirect) ERROR: {e}\n")

    # -----------------------
    # 2. HEAD (with redirects)
    # -----------------------
    try:
        t0 = time.time()
        r = requests.head(url, allow_redirects=True, timeout=10)
        dt = time.time() - t0
        print(f"HEAD (with redirect): {r.status_code} in {dt:.2f}s")
        print(f"Final URL: {r.url}")
        print(f"History: {[resp.status_code for resp in r.history]}")
        print(f"Headers: {r.headers}\n")
    except Exception as e:
        print(f"HEAD (with redirect) ERROR: {e}\n")

    # -----------------------
    # 3. GET (stream, minimal)
    # -----------------------
    try:
        t0 = time.time()
        r = requests.get(url, stream=True, allow_redirects=True, timeout=10)
        dt = time.time() - t0
        print(f"GET (stream): {r.status_code} in {dt:.2f}s")
        print(f"Final URL: {r.url}")
        print(f"History: {[resp.status_code for resp in r.history]}")
        print(f"Headers: {r.headers}")

        # Try reading just 1 byte
        try:
            chunk = next(r.iter_content(1))
            print("Read 1 byte successfully")
        except Exception as e:
            print(f"Could not read content: {e}")

        print()

    except Exception as e:
        print(f"GET ERROR: {e}\n")


if __name__ == "__main__":
    # paste ONE failing asset URL here
    test_url = "https://data.eodc.eu/collections/AI4SAR_SIG0/EQUI7_EU020M/E048N012T3/SIG0_20231227T165056__VH_A146_E048N012T3_EU020M_V1M1R2_S1AIWGRDH_TUWIEN.tif"
    test_asset_url(test_url)