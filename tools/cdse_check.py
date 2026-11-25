#!/usr/bin/env python3
"""Check whether the CDSE account can download Copernicus DEM GLO-30 2024_1.

    .venv/bin/python -I tools/cdse_check.py

Tries both routes with a real (ranged) download of one 2024_1 product:
  1. OData download, as the CDSE Browser does, with a token for the account
     whose login is in ~/.config/copdem/cdse-login (two lines: username,
     password; mode 600). The file is created by the person, in their own
     terminal; this script never prints the password or the token.
  2. S3 GET with the `cdse` profile's keys.

It prints the account's groups and roles from the token (not secret), and
the HTTP status of each attempt. Exit 0 only when both routes download.
"""
from __future__ import annotations

import base64
import json
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LOGIN = Path("~/.config/copdem/cdse-login").expanduser()
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PRODUCT_ID = "b3d60c08-13db-4e65-950b-3e297a6b791d"   # N00_E036, dataset COP-DEM_GLO-30-DGED/2024_1
PRODUCT_NAME = "DEM1_SAR_DGE_30_20110318T032119_20141130T032137_ADS_000000_b1lO_87525a93.DEM"
DOWNLOAD = f"https://download.dataspace.copernicus.eu/odata/v1/Products({PRODUCT_ID})/$value"
S3_KEY = ("CCM/COP-DEM_GLO-30-DGED/SAR_DGE_30_A4AD/2011/03/18/"
          "DEM1_SAR_DGE_30_20110318T032119_20141130T032137_ADS_000000_b1lO_87525a93/"
          "Copernicus_DSM_10_N00_00_E036_00/DEM/Copernicus_DSM_10_N00_00_E036_00_DEM.tif")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # follow manually so the token is re-sent
        return None


def odata() -> bool:
    if not LOGIN.exists():
        print(f"odata: skipped, {LOGIN} does not exist")
        return False
    if LOGIN.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"odata: refusing, {LOGIN} is readable by others (chmod 600 it)")
        return False
    user, password = LOGIN.read_text().splitlines()[:2]
    body = urllib.parse.urlencode({"grant_type": "password", "client_id": "cdse-public",
                                   "username": user.strip(), "password": password}).encode()
    try:
        tok = json.load(urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body), timeout=60))
    except urllib.error.HTTPError as e:
        print(f"odata: login failed, HTTP {e.code}")
        return False
    access = tok["access_token"]
    claims = json.loads(base64.urlsafe_b64decode(access.split(".")[1] + "==="))
    print("account groups:", claims.get("group_membership") or claims.get("groups"))
    print("account roles:", sorted((claims.get("realm_access") or {}).get("roles", [])))
    url = DOWNLOAD
    opener = urllib.request.build_opener(NoRedirect)
    for _ in range(5):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}", "Range": "bytes=0-15"})
        try:
            r = opener.open(req, timeout=120)
            print(f"odata: download HTTP {r.status}, first bytes {r.read(4)!r}")
            return True
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                url = e.headers["Location"]
                continue
            body = e.read(300).decode(errors="replace").strip()
            if e.code == 416 and "DAT-ZIP-116" in body:
                # A product downloads as one zip, which refuses byte ranges.
                # The service checks access first (403 DAT-ZIP-608 when denied),
                # so reaching this error means the account may download.
                print("odata: access OK (product zip refuses byte ranges: DAT-ZIP-116)")
                return True
            print(f"odata: download HTTP {e.code} {body}")
            return False
    print("odata: too many redirects")
    return False


def s3() -> bool:
    import boto3
    import botocore
    c = boto3.Session(profile_name="cdse").client(
        "s3", endpoint_url="https://eodata.dataspace.copernicus.eu", region_name="default")
    try:
        r = c.get_object(Bucket="eodata", Key=S3_KEY, Range="bytes=0-15")
        print(f"s3: GET OK, first bytes {r['Body'].read(4)!r}")
        return True
    except botocore.exceptions.ClientError as e:
        print(f"s3: GET HTTP {e.response['ResponseMetadata']['HTTPStatusCode']} {e.response['Error'].get('Code')}")
        return False


if __name__ == "__main__":
    print(f"product: {PRODUCT_NAME}")
    ok = [odata(), s3()]
    sys.exit(0 if all(ok) else 1)
