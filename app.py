import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from threading import Lock
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DATA = Path(os.environ.get("VOACAP_DATA", "/usr/share/voacapl/itshfbc"))
ENGINE = os.environ.get("VOACAP_BIN", "/usr/bin/voacapl")
RELIABILITY_TARGET = .90
BANDS = {"80": 3.8, "60": 5.35, "40": 7.2, "30": 10.12, "20": 14.2,
         "17": 18.14, "15": 21.3, "12": 24.95, "10": 28.4}


def grid_center(grid):
    if not re.fullmatch(r"[A-R]{2}[0-9]{2}([A-X]{2})?", grid):
        raise ValueError("Use a four- or six-character Maidenhead grid.")
    lon = (ord(grid[0]) - 65) * 20 + int(grid[2]) * 2 - 180
    lat = (ord(grid[1]) - 65) * 10 + int(grid[3]) - 90
    if len(grid) == 4:
        return lat + .5, lon + 1
    return lat + (ord(grid[5]) - 64.5) / 24, lon + (ord(grid[4]) - 64.5) / 12


def coordinate(value, positive, negative):
    return f"{abs(value):5.2f}{positive if value >= 0 else negative}"


def forecast(grid, year, month, hour, ssn, origin="EM75LD"):
    txlat, txlon = grid_center(origin)
    rxlat, rxlon = grid_center(grid)
    if math.dist((txlat, txlon), (rxlat, rxlon)) < .015:
        raise ValueError("Choose a destination outside the starting grid.")
    # Match the packed CIRCUIT columns used by the packaged engine.
    coords = "".join(f"{value:>10}" for value in (
        coordinate(txlat, "N", "S"), coordinate(txlon, "E", "W"),
        coordinate(rxlat, "N", "S"), coordinate(rxlon, "E", "W"),
    ))
    template = (DATA / "run/voacapx.dat").read_text()
    template = re.sub(r"^MONTH.*$", f"MONTH      {year:4}{month:5.2f}", template, flags=re.M)
    template = re.sub(r"^SUNSPOT.*$", f"SUNSPOT   {ssn:5.1f}", template, flags=re.M)
    template = template.replace("73.0", "38.0").replace("500.0000", "  0.1000")
    template = re.sub(r"^FREQUENCY.*$", "FREQUENCY " + "".join(f"{x:5.2f}" for x in [*BANDS.values(), 0, 0]), template, flags=re.M)
    routes = {}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for entry in DATA.iterdir():
            if entry.name == "run":
                shutil.copytree(entry, work / entry.name)
            else:
                (work / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
        for direction, flag in (("short", "0"), ("long", "1")):
            path = direction[0].upper()
            circuit = f"CIRCUIT   {coords[4:]}  {path}     {flag}"
            deck = re.sub(r"^CIRCUIT.*$", circuit, template, flags=re.M)
            (work / "run/test.dat").write_text(deck)
            subprocess.run([ENGINE, "-s", str(work), "test.dat", "test.out"], check=True, timeout=15, capture_output=True)
            lines = (work / "run/test.out").read_text().splitlines()
            idx = next(i for i, line in enumerate(lines) if "AZIMUTHS" in line)
            bearing, _, _, distance = map(float, lines[idx + 1].split()[-4:])
            for i, line in enumerate(lines):
                if line.rstrip().endswith("FREQ") and float(line.split()[0]) % 24 == hour:
                    muf = float(line.split()[1])
                    rel = next(x.split() for x in lines[i:i + 30] if x.rstrip().endswith("REL"))
                    reliability = dict(zip(BANDS, map(float, rel[1:10])))
                    passing = [frequency for band, frequency in BANDS.items()
                               if reliability[band] >= RELIABILITY_TARGET]
                    routes[direction] = {
                        "bearing": bearing, "distance_km": distance, "muf_mhz": muf,
                        "target_mhz": round(muf * .8, 2),
                        "lowest_passing_test_frequency_mhz": min(passing, default=None),
                        "reliability": reliability,
                    }
                    break
            if direction not in routes:
                raise RuntimeError("VOACAP did not return the requested hour.")
    return {"origin": origin, "destination": grid, "mode": "SSB",
            "origin_coordinates": {"latitude": txlat, "longitude": txlon},
            "destination_coordinates": {"latitude": rxlat, "longitude": rxlon},
            "month": month, "hour_utc": hour, "ssn": ssn,
            "reliability_target": RELIABILITY_TARGET, "routes": routes}


HAMQTH_API = "https://www.hamqth.com/api/v2"
hamqth_lock = Lock()
hamqth_session = ("", 0.0)


def hamqth_json(path, **kwargs):
    with urlopen(Request(HAMQTH_API + path, **kwargs), timeout=10) as response:
        return json.load(response)


def hamqth_token(callsign, password, rejected=None):
    global hamqth_session
    with hamqth_lock:
        token, expiry = hamqth_session
        if token != rejected and time.monotonic() < expiry:
            return token
        result = hamqth_json(
            "/login",
            data=json.dumps({"callsign": callsign, "password": password}).encode(),
            headers={"Content-Type": "application/json"},
        )
        token = result["token"]
        hamqth_session = (token, time.monotonic() + max(0, result["expiresIn"] - 60))
        return token


def lookup_grid(call, callsign, password):
    token = hamqth_token(callsign, password)
    try:
        record = hamqth_json(f"/callbook/{call}?app=AF4OZ", headers={"Authorization": f"Bearer {token}"})
    except HTTPError as error:
        if error.code != 401:
            raise
        token = hamqth_token(callsign, password, rejected=token)
        record = hamqth_json(f"/callbook/{call}?app=AF4OZ", headers={"Authorization": f"Bearer {token}"})
    grid = ((record.get("location") or {}).get("grid") or "").upper()
    return grid if re.fullmatch(r"[A-R]{2}[0-9]{2}(?:[A-X]{2})?", grid) else None


def callbook_response(call):
    if not re.fullmatch(r"[A-Z0-9]{3,12}", call):
        return 400, {"error": "Enter a valid call sign."}
    callsign = os.environ.get("HAMQTH_CALLSIGN")
    password = os.environ.get("HAMQTH_PASSWORD")
    if not callsign or not password:
        missing = [
            name for name, value in (
                ("HAMQTH_CALLSIGN", callsign),
                ("HAMQTH_PASSWORD", password),
            ) if not value
        ]
        return 503, {
            "error": "HamQTH lookup has not been configured.",
            "missing_settings": missing,
        }
    try:
        return 200, {"call": call, "grid": lookup_grid(call, callsign, password), "source": "HamQTH"}
    except HTTPError as error:
        if error.code == 404 and "/callbook/" in error.url:
            return 200, {"call": call, "grid": None, "source": "HamQTH"}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 502, {"error": "HamQTH lookup is temporarily unavailable."}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        request = urlsplit(self.path)
        args = parse_qs(request.query)
        try:
            if request.path == "/lookup":
                status, payload = callbook_response(args.get("call", [""])[0].strip().upper())
                return self.send_json(status, payload)
            if request.path == "/health":
                payload = {"ready": Path(ENGINE).is_file() and DATA.is_dir()}
            elif request.path == "/forecast":
                grid = args.get("grid", [""])[0].upper()
                year, month, hour, ssn = (int(args.get(k, [d])[0]) for k, d in
                    (("year", 2026), ("month", 11), ("hour", 17), ("ssn", 100)))
                if not (1900 <= year <= 2100 and 1 <= month <= 12 and 0 <= hour <= 23 and 0 <= ssn <= 300):
                    raise ValueError("Year, month, UTC hour or sunspot number is out of range.")
                origin = args.get("origin", ["EM75LD"])[0].strip().upper()
                payload = forecast(grid, year, month, hour, ssn, origin)
            else:
                return self.send_error(404)
            status = 200
        except ValueError as error:
            status, payload = 400, {"error": str(error)}
        except (OSError, RuntimeError, subprocess.SubprocessError, StopIteration):
            status, payload = 502, {"error": "Forecast unavailable."}
        self.send_json(status, payload)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "https://af4oz.com")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
