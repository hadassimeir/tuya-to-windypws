# Tuya PWS → Windy Bridge

A Home Assistant [pyscript](https://github.com/custom-components/pyscript) app that pulls live readings from a Tuya-connected personal weather station and pushes them to [Windy Personal Weather Stations (PWS)](https://stations.windy.com/), so your station shows up on Windy's live map.

## Why?

Tuya smart weather stations only report to Tuya's own app and the Tuya cloud, with no built-in way to publish to third-party weather networks. This script closes that gap by running on your Home Assistant instance: every 5 minutes it queries the Tuya OpenAPI for the station's current data points, converts/derives the values Windy expects, and posts them to Windy's observation endpoint.

## How it works

```
Tuya-connected weather station
        │  (reports to Tuya cloud)
        ▼
   Tuya OpenAPI  ──►  tuya_pws.py (pyscript, runs on Home Assistant)
                          │  parses raw data points
                          │  converts units (km/h → m/s, lux → W/m², etc.)
                          │  calculates dew point if not provided
                          ▼
                     Windy PWS API  ──►  stations.windy.com (your station page)
```

The script is triggered automatically every 5 minutes via a built-in cron schedule, and can also be run on demand as a Home Assistant service.

## Features

- Fetches the current "device shadow" (live properties) of your Tuya weather station
- Converts wind speed and gust from km/h to m/s (and mph for Windy's imperial field)
- Converts light intensity (lux) to approximate solar radiation (W/m²)
- Calculates dew point locally if your station doesn't report it directly
- Decodes the wind direction from Tuya's raw (base64) data point
- Posts the resulting observation to Windy PWS every 5 minutes
- Logs each step (token request, raw Tuya data, parsed data, Windy payload and response) to the Home Assistant log for easy debugging

## Prerequisites

1. A Tuya-connected weather station already added and working in the Tuya Smart / Smart Life app
2. A [Tuya IoT Platform](https://iot.tuya.com) account and Cloud Project (Access ID + Access Secret)
3. A [Windy PWS](https://stations.windy.com) account with a registered weather station (Station ID, API Key, Password)
4. Home Assistant with the [pyscript](https://github.com/custom-components/pyscript) integration installed
5. Basic comfort editing a Python file and Home Assistant's `configuration.yaml`

---

## Step 1 — Create a Tuya IoT Cloud Project

Tuya devices don't expose an API by default — you need a free Tuya IoT Platform project to get API access to your device.

1. Go to [iot.tuya.com](https://iot.tuya.com) and sign up (this is a separate account from the Tuya Smart / Smart Life mobile app).
2. In the left menu, go to **Cloud → Development → Create Cloud Project**.
3. Fill in:
   - **Project Name**: anything, e.g. `HA-Weather-Bridge`
   - **Development Method**: `Smart Home`
   - **Data Center**: pick the region matching where your Tuya app account is registered (Americas, Central Europe, Western Europe, China, or India). This determines which API host you'll use later.
4. Once created, you'll land on the project's **Overview** tab. Note down:
   - **Access ID / Client ID**
   - **Access Secret / Client Secret**
5. Go to the **Service API** tab and make sure the **IoT Core** API service is subscribed (it usually is, by default, on the free trial). This is what allows the script to fetch device status.
6. Go to the **Devices** tab → **Link Tuya App Account** → **Add App Account**, and scan the QR code using the Tuya Smart / Smart Life app on your phone (the same account your weather station is paired to). Once linked, your devices will appear under **All Devices**.
7. Click into your weather station device and copy its **Device ID**.
8. (Recommended) Use the **Cloud → API Explorer** tool in the Tuya console to manually call `GET /v2.0/cloud/thing/{device_id}/shadow/properties` with your device ID. This confirms your credentials work and lets you see the exact `code` names your station model reports — useful if your station isn't the same model this script was originally written for (see [Adapting to a different station model](#adapting-to-a-different-station-model) below).

You should now have: **Access ID**, **Access Secret**, **Device ID**, and the correct **API host** for your region:

| Region | API Host |
|---|---|
| Americas | `https://openapi.tuyaus.com` |
| Europe | `https://openapi.tuyaeu.com` |
| China | `https://openapi.tuyacn.com` |
| India | `https://openapi.tuyain.com` |

## Step 2 — Create a Windy PWS Station

1. Create an account at [windy.com](https://www.windy.com) if you don't have one.
2. Go to [stations.windy.com](https://stations.windy.com) and click **Add Station**.
3. Fill in your station's details — name, latitude/longitude, elevation, and station type (choose "Other"/custom hardware, since this isn't an officially integrated brand).
4. Once the station is created, open its **Settings** page and note down:
   - **Station ID**
   - **API Key**
   - **Password**

These three values authenticate the script's requests to Windy — full API reference: [stations.windy.com/pws/api](https://stations.windy.com/pws/api).

## Step 3 — Install PyScript on Home Assistant

If you don't already have pyscript installed:

1. Easiest method: install **pyscript** via [HACS](https://hacs.xyz/) (Settings → search "pyscript" → Install), or install it manually by copying its `custom_components/pyscript` folder into your Home Assistant `config/custom_components/` directory.
2. In Home Assistant, go to **Settings → Devices & Services → Add Integration**, search for **Pyscript Python scripting**, and add it.
3. Edit your `configuration.yaml` to allow the `requests` library to be imported (required by this script) and expose HA objects globally:

   ```yaml
   pyscript:
     allow_all_imports: true
     hass_is_global: true
   ```

4. Restart Home Assistant to apply the config change.

## Step 4 — Install the Script

1. In your Home Assistant `config` folder, create a `pyscript` folder if it doesn't already exist.
2. Copy `tuya_pws.py` into `<config>/pyscript/tuya_pws.py`.
3. Open the file and edit the **CONFIGURATION** section near the top with your own values from Steps 1 and 2:

   ```python
   # --- Tuya Cloud project credentials ---
   TUYA_ACCESS_ID = 'your_tuya_access_id'
   TUYA_ACCESS_SECRET = 'your_tuya_access_secret'
   TUYA_API_HOST = 'https://openapi.tuyaeu.com'   # match your data center region
   TUYA_DEVICE_ID = 'your_device_id'

   # --- Windy PWS credentials ---
   WINDY_API_KEY = 'your_windy_api_key'
   WINDY_PASSWORD = 'your_windy_password'
   WINDY_STATION_ID = 'your_station_id'           
   ```

4. Save the file. Pyscript automatically picks up new/changed files, or you can force a reload from **Developer Tools → YAML → Reload → Pyscript**.

## Step 5 — Verify It's Working

1. Go to **Developer Tools → Actions** (or **Services**, depending on your HA version), and call:

   ```
   pyscript.tuya_pws_update
   ```

2. Check the Home Assistant logs (**Settings → System → Logs**, or search for `pyscript.file.tuya_pws`) — the script logs each stage: the Tuya access token, raw device data, parsed/converted values, the final Windy payload, and Windy's HTTP response.
3. If everything is correct, your station should appear as reporting live data on its [stations.windy.com](https://stations.windy.com) settings page within a minute or two.

From this point on, the script runs automatically every 5 minutes via its built-in cron trigger — no external automation is required.

---

## Data Sent to Windy

| Windy field | Source |
|---|---|
| `temp` | Outdoor temperature (°C) |
| `wind` / `windspeedmph` | Wind speed (converted from km/h to m/s, plus mph) |
| `gust` | Wind gust speed (m/s) |
| `winddir` | Wind direction (degrees), decoded from Tuya's raw data point |
| `humidity` | Outdoor humidity (%) |
| `dewpoint` | Dew point (°C) — from the sensor if available, otherwise calculated |
| `mbar` | Atmospheric pressure (hPa/mbar) |
| `precip` | Rainfall, last hour (mm) |
| `uv` | UV index |
| `solarradiation` | Approximate solar radiation (W/m²), converted from lux |

## Adapting to a Different Station Model

Tuya doesn't standardize data point (`code`) names across weather station models. This script was built against one specific model's DP list (`TUYA_CODE_MAP` in the script). If your station reports different codes:

1. Use the Tuya **API Explorer** (see Step 1.8) to call `shadow/properties` for your device and see the actual `code` values it returns.
2. Update `TUYA_CODE_MAP` in the script to match — each entry maps a Tuya `code` to a friendly field name and the divisor needed to scale the raw integer into real units (Tuya typically reports values ×10 or ×100 to avoid floats).

### ⚠️ A note on wind direction

Tuya reports wind direction as a base64-encoded raw blob rather than a plain number, and its layout isn't publicly documented. The `decode_wind_direction_degrees()` function in this script was reverse-engineered from a handful of sample payloads on one station model — **it is not guaranteed to be correct for other models**. If your wind direction readings look wrong, compare against your station's on-screen compass and adjust the byte offset in that function accordingly.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Tuya token request fails / `success: false` | Wrong `TUYA_ACCESS_ID`/`TUYA_ACCESS_SECRET`, or wrong `TUYA_API_HOST` for your region |
| Device status request fails or returns empty | Device not linked to your cloud project (redo Step 1.6), or wrong `TUYA_DEVICE_ID` |
| Some fields missing from the Windy payload | Your station doesn't report that data point, or its `code` isn't in `TUYA_CODE_MAP` — see above |
| Windy shows no incoming data | Double-check `WINDY_API_KEY`, `WINDY_PASSWORD`, and `WINDY_STATION_ID`; check the logged HTTP response body for Windy's error message |
| `requests` import fails | `allow_all_imports: true` missing from the `pyscript:` block in `configuration.yaml` |

## License

[MIT](https://choosealicense.com/licenses/mit/)
