"""
tuya_pws.py

HA pyscript app: fetches live readings from a Tuya-connected weather
station via the Tuya OpenAPI, parses/converts the data points, computes
any derived values (dew point), builds a Windy PWS payload and pushes it
to Windy's v2 observation/update endpoint
(stations.windy.com/api/v2/observation/update).

Place this file in <config>/pyscript/tuya_pws.py. Requires
`allow_all_imports: true` under the pyscript integration config so that
`requests` can be imported. Exposes the pyscript.tuya_pws_update service;
call it from a HA automation/script, or add a @time_trigger below for an
internal cron schedule instead of relying on external cron.
"""

import base64
import hashlib
import hmac
import math
import struct
import time
from urllib.parse import urlencode

import requests

# ============================================================================
# 1. CONFIGURATION -- edit these values for your account / device
# ============================================================================

# --- Tuya Cloud project credentials (https://iot.tuya.com -> Cloud -> Project) ---
TUYA_ACCESS_ID = 'TUYA_ACCESS_ID'
TUYA_ACCESS_SECRET = 'TUYA_ACCESS_SECRET'

# Pick the data-center host that matches where your Tuya project lives:
#   Americas: https://openapi.tuyaus.com
#   Europe:   https://openapi.tuyaeu.com
#   China:    https://openapi.tuyacn.com
#   India:    https://openapi.tuyain.com
TUYA_API_HOST = 'https://openapi.tuyaeu.com'

# The device id of the weather station, as shown in the Tuya IoT console.
TUYA_DEVICE_ID = 'TUYA_DEVICE_ID'

# --- Windy PWS credentials (https://stations.windy.com -> your station -> settings) ---
WINDY_ENDPOINT = 'https://stations.windy.com/api/v2/observation/update'
WINDY_API_KEY = 'WINDY_API_KEY'
WINDY_PASSWORD = 'WINDY_PASSWORD'
WINDY_STATION_ID = 'WINDY_STATION_ID'  

# Map of Tuya "code" -> raw-value divisor. Confirmed against this exact
# weather-station model's DP list .
TUYA_CODE_MAP = {
    'temp_current_external': {'field': 'temp_c', 'divisor': 10},
    'Wind_speed': {'field': 'wind_kmh', 'divisor': 10},  # km/h -> converted to m/s below
    'windspeed_gust': {'field': 'gust_kmh', 'divisor': 10},  # km/h -> converted to m/s below
    'humidity_outdoor': {'field': 'humidity', 'divisor': 1},
    'dew_point_temp': {'field': 'dewpoint_c', 'divisor': 10},
    'atmospheric_pressture': {'field': 'pressure_hpa', 'divisor': 1},
    'rain_1h': {'field': 'rain_mm', 'divisor': 10},
    'uv_index': {'field': 'uv', 'divisor': 1},
    'Light_intensity': {'field': 'light_lux', 'divisor': 1},  # converted Lux -> W/m2 below
}

# Wind direction is NOT a plain scaled number -- Tuya reports it as a
# base64-encoded raw blob (note the vendor's own typo: "Wing_direction").
# It's decoded separately by decode_wind_direction_degrees() below.
TUYA_WIND_DIRECTION_CODE = 'Wing_direction'


# ============================================================================
# 2. TUYA SIGNING HELPERS
# ============================================================================

def tuya_sign(method: str, path: str, body: str, token, t: str, nonce: str) -> str:
    """Builds the signature Tuya expects (HMAC-SHA256, uppercase hex).

    method  -- HTTP method, e.g. GET / POST
    path    -- Request path + query string, e.g. /v1.0/token?grant_type=1
    body    -- Raw request body (empty string for GET)
    token   -- Access token; None for the initial token request
    t       -- Millisecond timestamp used in the request
    nonce   -- Arbitrary nonce (can be empty string)
    """
    content_sha256 = hashlib.sha256(body.encode('utf-8')).hexdigest()
    headers_to_sign = ''  # no signed headers used here
    string_to_sign = f"{method}\n{content_sha256}\n{headers_to_sign}\n{path}"

    string = TUYA_ACCESS_ID + (token or '') + t + nonce + string_to_sign

    return hmac.new(
        TUYA_ACCESS_SECRET.encode('utf-8'),
        string.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest().upper()


def tuya_request(method: str, path: str, token=None, body: str = '') -> dict:
    """Performs an HTTP request to the Tuya OpenAPI, signed appropriately."""
    t = str(round(time.time() * 1000))
    nonce = ''
    sign = tuya_sign(method, path, body, token, t, nonce)

    headers = {
        'client_id': TUYA_ACCESS_ID,
        'sign': sign,
        't': t,
        'sign_method': 'HMAC-SHA256',
        'nonce': nonce,
        'Content-Type': 'application/json',
    }
    if token is not None:
        headers['access_token'] = token

    try:
        # task.executor runs the blocking call in a worker thread so it
        # doesn't stall HA's event loop.
        resp = task.executor(
            requests.request,
            method,
            TUYA_API_HOST + path,
            headers=headers,
            data=body if body else None,
            timeout=15,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Tuya HTTP request failed: {e}")

    try:
        decoded = resp.json()
    except ValueError:
        raise RuntimeError(f"Tuya returned non-JSON response: {resp.text}")

    if not isinstance(decoded, dict):
        raise RuntimeError(f"Tuya returned non-JSON response: {resp.text}")
    return decoded


def get_tuya_access_token() -> str:
    """Obtains a Tuya access token using the "grant_type=1" (project credential) flow."""
    path = '/v1.0/token?grant_type=1'
    resp = tuya_request('GET', path)

    if not resp.get('success'):
        raise RuntimeError(f"Tuya token request failed: {resp}")
    return resp['result']['access_token']


def get_tuya_device_status(token: str, device_id: str) -> list:
    """Retrieves the current status (data points) of the given device via the
    v2.0 "device shadow" properties endpoint.
    """
    path = f"/v2.0/cloud/thing/{device_id}/shadow/properties"
    resp = tuya_request('GET', path, token)

    if not resp.get('success'):
        raise RuntimeError(f"Tuya device status request failed: {resp}")
    return resp['result']['properties']  # list of {'code': ..., 'value': ...}


# ============================================================================
# 3. PARSING / DERIVED CALCULATIONS
# ============================================================================

def parse_tuya_status(status_list: list) -> dict:
    """Converts the raw Tuya status list into a clean, scaled, keyed dict
    using TUYA_CODE_MAP. The wind-direction code is handled separately
    since it's a base64-encoded raw blob, not a plain scaled number.
    """
    parsed = {}
    for point in status_list:
        code = point.get('code')
        if code is None:
            continue

        if code == TUYA_WIND_DIRECTION_CODE:
            degrees = decode_wind_direction_degrees(str(point['value']))
            if degrees is not None:
                parsed['wind_dir'] = degrees
            continue

        if code not in TUYA_CODE_MAP:
            continue  # unmapped data point, ignore
        mapping = TUYA_CODE_MAP[code]
        value = point['value']
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed[mapping['field']] = value / mapping['divisor']
        else:
            parsed[mapping['field']] = value
    return parsed


def decode_wind_direction_degrees(base64_value: str):
    """Decodes the "Wing_direction" raw datapoint into degrees (0-360).

    Tuya does not publicly document this raw layout. Based on inspecting
    real payloads from this weather-station model, the direction sits at
    byte offset 5-6 (not 0-1, which is where an earlier version of this
    function looked), as an unsigned big-endian short:
      base64("AE5XAAABReAB") -> 00 4e 57 00 00 [01 45] e0 01 -> 325 degrees
      base64("AABOAAABZuAB") -> 00 00 4e 00 00 [01 66] e0 01 -> 358 degrees
      base64("AE5XAAABPsAF") -> 00 4e 57 00 00 [01 3e] c0 05 -> 318 degrees
    The first 5 bytes and last 2 bytes are of unknown meaning (possibly wind
    speed/gust echoes or a status/checksum byte) and are ignored here.
    A value of 0xFFFF at that offset means "no valid direction" (seen during
    calm/no-wind conditions), not 0 degrees:
      base64("AABDAAD//wAA") -> 00 00 43 00 00 [ff ff] 00 00 -> invalid (calm)

    IMPORTANT: this is inferred from limited samples, not official docs.
    Verify against your device's on-screen compass reading; if it doesn't
    line up, adjust the byte offset/endianness below.
    """
    try:
        raw_bytes = base64.b64decode(base64_value, validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw_bytes) < 7:
        return None

    degrees = struct.unpack('>H', raw_bytes[5:7])[0]  # '>H' = unsigned short, big-endian
    if degrees == 0xFFFF:
        return None  # calm / no valid direction
    return degrees % 360


def calculate_dew_point_c(temp_c: float, humidity_pct: float) -> float:
    """Magnus-Tetens approximation for dew point (input/output in Celsius)."""
    a = 17.62
    b = 243.12
    gamma = (a * temp_c) / (b + temp_c) + math.log(humidity_pct / 100)
    return (b * gamma) / (a - gamma)


def ms_to_mph(ms: float) -> float:
    return ms * 2.2369362920544


def kmh_to_ms(kmh: float) -> float:
    return kmh / 3.6


def lux_to_wm2(lux: float) -> float:
    """Approximate conversion from illuminance (lux) to solar irradiance (W/m^2).

    There's no exact physical constant here -- the ratio depends on the light
    spectrum -- but ~126.7 lux per W/m^2 is the commonly used approximation
    for daylight, and is what most PWS software (Ecowitt, WeeWX, etc.) uses.
    """
    return lux / 126.7


# ============================================================================
# 4. WINDY PAYLOAD + SUBMISSION
# ============================================================================

def build_windy_payload(data: dict) -> dict:
    """Builds the query-string payload Windy's v2 observation endpoint expects.
    Windy accepts metric fields directly and also wants the imperial
    equivalents alongside them (temp/tempf, wind/windspeedmph, precip/rainin).
    Docs: https://stations.windy.com/pws/api
    """
    payload = {
        'key': WINDY_API_KEY,
        'PASSWORD': WINDY_PASSWORD,
        'dateutc': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()),
    }

    if WINDY_STATION_ID is not None:
        payload['stationId'] = WINDY_STATION_ID

    if 'temp_c' in data:
        payload['temp'] = data['temp_c']

    if 'wind_ms' in data:
        payload['wind'] = data['wind_ms']
        payload['windspeedmph'] = round(ms_to_mph(float(data['wind_ms'])), 1)

    if 'wind_gust_ms' in data:
        payload['gust'] = data['wind_gust_ms']

    if 'wind_dir' in data:
        payload['winddir'] = data['wind_dir']

    if 'humidity' in data:
        payload['humidity'] = data['humidity']

    if 'dewpoint_c' in data:
        payload['dewpoint'] = data['dewpoint_c']

    if 'pressure_hpa' in data:
        payload['mbar'] = data['pressure_hpa']  # mbar and hPa are numerically identical

    if 'rain_mm' in data:
        payload['precip'] = data['rain_mm']

    if 'uv' in data:
        payload['uv'] = data['uv']

    if 'solarradiation' in data:
        payload['solarradiation'] = data['solarradiation']

    # Drop null fields so Windy doesn't choke on empty params.
    return {k: v for k, v in payload.items() if v is not None}


def send_to_windy(payload: dict) -> dict:
    """Sends the payload to Windy and returns the raw response body + HTTP code."""
    url = WINDY_ENDPOINT + '?' + urlencode(payload)

    try:
        resp = task.executor(requests.get, url, timeout=15)
    except requests.RequestException as e:
        raise RuntimeError(f"Windy HTTP request failed: {e}")

    return {'http_code': resp.status_code, 'body': resp.text, 'url': url}


# ============================================================================
# 5. MAIN
# ============================================================================

@service
@time_trigger("cron(*/5 * * * *)")
def tuya_pws_update():
    """yaml
name: Tuya PWS update
description: Fetch the latest reading from the Tuya weather station and push it to Windy PWS.
"""
    try:
        print("==== 1. Requesting Tuya access token ====")
        token = get_tuya_access_token()
        print(f"Access token acquired: {token}\n")

        print("==== 2. Fetching device status from Tuya ====")
        raw_status = get_tuya_device_status(token, TUYA_DEVICE_ID)
        print("Raw Tuya status:")
        print(raw_status)
        print("")

        print("==== 3. Parsed / scaled values ====")
        parsed = parse_tuya_status(raw_status)
        print(parsed)
        print("")

        print("==== 4. Derived calculations ====")
        if 'wind_kmh' in parsed:
            parsed['wind_ms'] = round(kmh_to_ms(float(parsed['wind_kmh'])), 2)
            print(f"Wind speed: {parsed['wind_kmh']} km/h -> {parsed['wind_ms']} m/s")
        if 'gust_kmh' in parsed:
            parsed['wind_gust_ms'] = round(kmh_to_ms(float(parsed['gust_kmh'])), 2)
            print(f"Wind gust: {parsed['gust_kmh']} km/h -> {parsed['wind_gust_ms']} m/s")
        if 'light_lux' in parsed:
            parsed['solarradiation'] = round(lux_to_wm2(float(parsed['light_lux'])), 1)
            print(f"Light intensity: {parsed['light_lux']} lux -> {parsed['solarradiation']} W/m^2")
        if 'dewpoint_c' in parsed:
            print(f"Dew point (from sensor): {parsed['dewpoint_c']} C")
        elif 'temp_c' in parsed and 'humidity' in parsed:
            parsed['dewpoint_c'] = round(calculate_dew_point_c(float(parsed['temp_c']), float(parsed['humidity'])), 1)
            print(f"Dew point (calculated fallback): {parsed['dewpoint_c']} C")
        else:
            print("Skipping dew point (no sensor value and insufficient data to calculate)")
        print("")

        print("==== 5. Windy payload ====")
        payload = build_windy_payload(parsed)
        print(payload)
        print("")

        print("==== 6. Sending to Windy ====")
        result = send_to_windy(payload)
        print(f"Request URL : {result['url']}")
        print(f"HTTP status : {result['http_code']}")
        print(f"Response    : {result['body']}")
    except Exception as e:
        log.error(f"tuya_pws_update failed: {e}")
