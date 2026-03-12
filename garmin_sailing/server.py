"""Garmin Sailing MCP Server.

Sailing-focused MCP server that combines Garmin Connect GPS data
with Open-Meteo historical weather to provide sailing analytics.
"""

import json
import math
from datetime import datetime, timezone

import httpx
from fastmcp import FastMCP
from fastmcp.server.apps import AppConfig, ResourceCSP

from garmin_sailing.auth import get_client

# ---------------------------------------------------------------------------
# Initialize
# ---------------------------------------------------------------------------
garmin = get_client()
mcp = FastMCP("Garmin Sailing")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MS_TO_KNOTS = 1.94384
M_TO_NM = 0.000539957

# ---------------------------------------------------------------------------
# Weather helpers
# ---------------------------------------------------------------------------

def _fetch_historical_weather(lat: float, lon: float, date: str) -> dict:
    """Fetch hourly wind & weather from Open-Meteo for a given date."""
    resp = httpx.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude": round(lat, 4),
            "longitude": round(lon, 4),
            "start_date": date,
            "end_date": date,
            "hourly": ",".join([
                "wind_speed_10m",
                "wind_direction_10m",
                "wind_gusts_10m",
                "temperature_2m",
                "weather_code",
                "precipitation",
                "rain",
                "showers",
                "cloud_cover",
                "cape",
                "visibility",
            ]),
            "wind_speed_unit": "kn",
            "timezone": "auto",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _get_weather_for_range(weather: dict, start_hour: int, end_hour: int) -> dict:
    """Get weather values covering the activity time range."""
    hourly = weather.get("hourly", {})
    times = hourly.get("time", [])
    n = len(times)
    start_idx = min(start_hour, n - 1)
    end_idx = min(end_hour, n - 1)

    def _val(key: str, idx: int):
        vals = hourly.get(key, [])
        return vals[idx] if idx < len(vals) else None

    def _max_in_range(key: str):
        vals = hourly.get(key, [])
        subset = [v for v in vals[start_idx:end_idx + 1] if v is not None]
        return max(subset) if subset else None

    def _sum_in_range(key: str):
        vals = hourly.get(key, [])
        subset = [v for v in vals[start_idx:end_idx + 1] if v is not None]
        return round(sum(subset), 2) if subset else None

    return {
        "wind_speed_knots": _val("wind_speed_10m", start_idx),
        "wind_direction_deg": _val("wind_direction_10m", start_idx),
        "wind_gusts_knots": _max_in_range("wind_gusts_10m"),
        "temperature_c": _val("temperature_2m", start_idx),
        "weather_code": _val("weather_code", start_idx),
        "precipitation_mm": _sum_in_range("precipitation"),
        "rain_mm": _sum_in_range("rain"),
        "showers_mm": _sum_in_range("showers"),
        "cloud_cover_pct": _max_in_range("cloud_cover"),
        "cape_jkg": _max_in_range("cape"),
        "visibility_m": _val("visibility", start_idx),
    }


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate bearing in degrees between two GPS points."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _angle_diff(a: float, b: float) -> float:
    """Smallest angle between two bearings (0-180)."""
    diff = abs(a - b) % 360
    return diff if diff <= 180 else 360 - diff


def _classify_point_of_sail(heading: float, wind_dir: float) -> str:
    """Classify the point of sail based on angle to wind."""
    angle = _angle_diff(heading, wind_dir)
    if angle < 45:
        return "into_wind"
    elif angle < 70:
        return "close_hauled"
    elif angle < 110:
        return "beam_reach"
    elif angle < 150:
        return "broad_reach"
    else:
        return "running"


# ---------------------------------------------------------------------------
# Track extraction
# ---------------------------------------------------------------------------

def _build_track_points(details: dict) -> list[dict]:
    """Extract GPS track points from Garmin activity details."""
    descriptors = details.get("metricDescriptors", [])
    key_index = {d["key"]: i for i, d in enumerate(descriptors)}
    metrics = details.get("activityDetailMetrics", [])

    points = []
    for entry in metrics:
        m = entry.get("metrics", [])
        lat = m[key_index["directLatitude"]] if "directLatitude" in key_index else None
        lon = m[key_index["directLongitude"]] if "directLongitude" in key_index else None
        if lat is None or lon is None:
            continue
        points.append({
            "lat": lat,
            "lon": lon,
            "timestamp": m[key_index["directTimestamp"]] if "directTimestamp" in key_index else None,
            "speed_ms": m[key_index["directSpeed"]] if "directSpeed" in key_index else 0,
            "heart_rate": m[key_index["directHeartRate"]] if "directHeartRate" in key_index else None,
            "distance_m": m[key_index["sumDistance"]] if "sumDistance" in key_index else None,
        })
    return points


# ---------------------------------------------------------------------------
# Sailing analysis
# ---------------------------------------------------------------------------

def _get_activity_datetime(track_points: list[dict]) -> datetime | None:
    """Get activity start datetime from first track point."""
    ts = track_points[0].get("timestamp") if track_points else None
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts else None


def _get_duration_min(track_points: list[dict]) -> float:
    """Estimate duration in minutes from track timestamps."""
    t_start = track_points[0].get("timestamp", 0)
    t_end = track_points[-1].get("timestamp", 0)
    return (t_end - t_start) / 60_000 if t_start and t_end else 0


def _analyze_sailing(
    points: list[dict], weather: dict, activity_hour: int, duration_min: float
) -> dict:
    """Produce sailing-specific analysis from GPS points and weather."""
    if len(points) < 2:
        return {"error": "Not enough GPS points for analysis"}

    end_hour = activity_hour + max(1, int(duration_min / 60) + 1)
    wx = _get_weather_for_range(weather, activity_hour, end_hour)
    wind_dir = wx.get("wind_direction_deg")

    speeds_knots = []
    headings = []
    points_of_sail = []
    heading_changes = []

    for i in range(1, len(points)):
        p0, p1 = points[i - 1], points[i]
        speed_knots = (p1.get("speed_ms") or 0) * MS_TO_KNOTS
        speeds_knots.append(speed_knots)

        heading = _bearing(p0["lat"], p0["lon"], p1["lat"], p1["lon"])
        headings.append(heading)

        if wind_dir is not None:
            points_of_sail.append(_classify_point_of_sail(heading, wind_dir))

        if len(headings) >= 2:
            heading_changes.append(_angle_diff(headings[-2], headings[-1]))

    # Distance
    total_distance_m = points[-1].get("distance_m") or 0
    first_distance = points[0].get("distance_m") or 0
    total_distance_nm = (total_distance_m - first_distance) * M_TO_NM

    # Duration
    t_start = points[0].get("timestamp")
    t_end = points[-1].get("timestamp")
    dur = (t_end - t_start) / 60_000 if t_start and t_end else 0

    # Speed
    avg_speed = sum(speeds_knots) / len(speeds_knots) if speeds_knots else 0
    max_speed = max(speeds_knots) if speeds_knots else 0

    # Heart rate
    hrs = [p["heart_rate"] for p in points if p.get("heart_rate")]
    avg_hr = round(sum(hrs) / len(hrs)) if hrs else None

    # Maneuvers (heading change > 60°)
    maneuvers = sum(1 for hc in heading_changes if hc > 60)

    # Point of sail distribution
    sail_dist = {}
    for pos in points_of_sail:
        sail_dist[pos] = sail_dist.get(pos, 0) + 1
    total_seg = len(points_of_sail) or 1
    sail_pct = {k: round(v / total_seg * 100, 1) for k, v in sail_dist.items()}

    # VMG
    vmg_values = []
    if wind_dir is not None:
        for i, heading in enumerate(headings):
            angle_to_wind = math.radians(_angle_diff(heading, wind_dir))
            vmg_values.append(speeds_knots[i] * math.cos(angle_to_wind))
    avg_vmg = sum(vmg_values) / len(vmg_values) if vmg_values else None

    return {
        "track_summary": {
            "distance_nm": round(total_distance_nm, 2),
            "duration_minutes": round(dur, 1),
            "avg_speed_knots": round(avg_speed, 2),
            "max_speed_knots": round(max_speed, 2),
            "total_gps_points": len(points),
        },
        "wind_conditions": {
            "wind_speed_knots": wx.get("wind_speed_knots"),
            "wind_direction_deg": wind_dir,
            "wind_gusts_knots": wx.get("wind_gusts_knots"),
            "temperature_c": wx.get("temperature_c"),
            "weather_code": wx.get("weather_code"),
        },
        "storm_indicators": {
            "precipitation_mm": wx.get("precipitation_mm"),
            "rain_mm": wx.get("rain_mm"),
            "showers_mm": wx.get("showers_mm"),
            "cloud_cover_pct": wx.get("cloud_cover_pct"),
            "cape_jkg": wx.get("cape_jkg"),
            "visibility_m": wx.get("visibility_m"),
        },
        "sailing_analysis": {
            "avg_vmg_knots": round(avg_vmg, 2) if avg_vmg is not None else None,
            "maneuvers_detected": maneuvers,
            "point_of_sail_pct": sail_pct,
        },
        "heart_rate": {
            "avg_bpm": avg_hr,
            "max_bpm": max(hrs) if hrs else None,
        },
    }


def _fetch_and_analyze(activity_id: str) -> tuple[list[dict], dict]:
    """Shared logic: fetch Garmin data + weather, return (track_points, analysis)."""
    details = garmin.get_activity_details(activity_id)
    track_points = _build_track_points(details)
    if not track_points:
        return [], {"error": "No GPS data found for this activity"}

    dt = _get_activity_datetime(track_points)
    if not dt:
        return [], {"error": "No timestamp data in activity"}

    weather = _fetch_historical_weather(
        track_points[0]["lat"], track_points[0]["lon"], dt.strftime("%Y-%m-%d")
    )
    duration_min = _get_duration_min(track_points)
    analysis = _analyze_sailing(track_points, weather, dt.hour, duration_min)
    return track_points, analysis


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool
def get_sailing_activities(limit: int = 10) -> list[dict]:
    """
    List recent sailing activities from Garmin Connect.

    - **limit**: Number of activities to return (default 10, max 100).

    Returns sailing activities with: name, date, distance (nm),
    duration (minutes), avg speed (knots), heart rate, and calories.
    """
    # Garmin API doesn't support sailing_v2 as a filter, so we fetch more
    # activities and filter client-side
    all_activities = garmin.get_activities(0, 100)
    sailing = [
        a for a in all_activities
        if a.get("activityType", {}).get("typeKey") == "sailing_v2"
    ][:limit]
    return [
        {
            "activity_id": a.get("activityId"),
            "name": a.get("activityName"),
            "date": a.get("startTimeLocal"),
            "distance_nm": round(a.get("distance", 0) / 1000 * M_TO_NM * 1000, 2),
            "duration_minutes": round(a.get("duration", 0) / 60, 2),
            "avg_speed_knots": round(a.get("averageSpeed", 0) * MS_TO_KNOTS, 2),
            "avg_heart_rate": a.get("averageHR"),
            "max_heart_rate": a.get("maxHR"),
            "calories": a.get("calories"),
        }
        for a in sailing
    ]


@mcp.tool
def get_sailing_activity(activity_id: str) -> dict:
    """
    Get detailed sailing analysis for a Garmin activity.

    Combines Garmin GPS data with Open-Meteo historical weather.

    - **activity_id**: The activity ID (from get_sailing_activities).

    Returns:
    - **track_summary**: distance (nm), duration, avg/max speed (knots)
    - **wind_conditions**: wind speed, direction, gusts (knots), temperature
    - **storm_indicators**: precipitation, cloud cover, CAPE
    - **sailing_analysis**: VMG, tack/jibe count, point of sail distribution
    - **heart_rate**: avg and max BPM
    """
    _, analysis = _fetch_and_analyze(activity_id)
    return analysis


SAILING_MAP_URI = "ui://sailing/map.html"


@mcp.tool(app=AppConfig(resource_uri=SAILING_MAP_URI))
def get_sailing_map(activity_id: str) -> str:
    """
    Show an interactive map of a sailing activity.

    - **activity_id**: The activity ID (from get_sailing_activities).

    Renders a Leaflet map with:
    - GPS track colored by speed (blue=slow, red=fast)
    - Wind arrow showing direction and strength
    - Start/end markers
    - Stats overlay with sailing metrics
    """
    track_points, analysis = _fetch_and_analyze(activity_id)
    if not track_points:
        return json.dumps(analysis)

    dt = _get_activity_datetime(track_points)
    track_for_map = []
    for i, p in enumerate(track_points):
        point = {
            "lat": p["lat"],
            "lon": p["lon"],
            "speed_knots": round((p.get("speed_ms") or 0) * MS_TO_KNOTS, 2),
        }
        if i > 0:
            point["heading"] = round(
                _bearing(
                    track_points[i - 1]["lat"], track_points[i - 1]["lon"],
                    p["lat"], p["lon"],
                ), 1
            )
        track_for_map.append(point)

    return json.dumps({
        "track": track_for_map,
        "analysis": analysis,
        "activity_date": dt.strftime("%Y-%m-%d") if dt else "",
    })


@mcp.resource(
    SAILING_MAP_URI,
    app=AppConfig(
        csp=ResourceCSP(
            resource_domains=[
                "https://unpkg.com",
                "https://tile.openstreetmap.org",
            ],
            connect_domains=[
                "https://tile.openstreetmap.org",
            ],
        ),
    ),
)
def sailing_map_view() -> str:
    """Interactive sailing map with GPS trace and wind overlay."""
    return """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light dark">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body { width: 100%; height: 600px; min-height: 600px;
                 font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
    #map { width: 100%; height: 100%; min-height: 600px; }

    .stats-panel {
      position: absolute; top: 10px; right: 10px; z-index: 1000;
      background: rgba(255,255,255,0.95); border-radius: 10px;
      padding: 14px 18px; min-width: 220px;
      box-shadow: 0 2px 12px rgba(0,0,0,0.15);
      font-size: 13px; line-height: 1.6;
    }
    @media (prefers-color-scheme: dark) {
      .stats-panel { background: rgba(30,30,30,0.95); color: #eee; }
    }
    .stats-panel h3 { margin-bottom: 6px; font-size: 15px; }
    .stats-panel .label { color: #888; }
    .stats-panel .value { font-weight: 600; float: right; }

    .wind-panel {
      position: absolute; bottom: 30px; right: 10px; z-index: 1000;
      background: rgba(255,255,255,0.95); border-radius: 10px;
      padding: 14px; box-shadow: 0 2px 12px rgba(0,0,0,0.15);
      text-align: center; min-width: 120px;
    }
    @media (prefers-color-scheme: dark) {
      .wind-panel { background: rgba(30,30,30,0.95); color: #eee; }
    }
    .wind-panel .arrow {
      font-size: 40px; display: inline-block;
      transition: transform 0.3s;
    }
    .wind-panel .wind-label { font-size: 12px; color: #888; margin-top: 4px; }
    .wind-panel .wind-speed { font-size: 16px; font-weight: 700; }

    .legend {
      position: absolute; bottom: 30px; left: 10px; z-index: 1000;
      background: rgba(255,255,255,0.95); border-radius: 10px;
      padding: 10px 14px; box-shadow: 0 2px 12px rgba(0,0,0,0.15);
      font-size: 12px;
    }
    @media (prefers-color-scheme: dark) {
      .legend { background: rgba(30,30,30,0.95); color: #eee; }
    }
    .legend-bar {
      width: 120px; height: 12px; border-radius: 6px;
      background: linear-gradient(to right, #313695, #4575b4, #74add1,
        #abd9e9, #fee090, #fdae61, #f46d43, #d73027);
      margin: 4px 0;
    }
    .legend-labels { display: flex; justify-content: space-between; font-size: 10px; }
  </style>
</head>
<body>
  <div id="map"></div>

  <div class="stats-panel" id="stats">
    <h3>Loading...</h3>
  </div>

  <div class="wind-panel" id="wind-panel" style="display:none;">
    <div class="wind-label">WIND</div>
    <div class="arrow" id="wind-arrow">&#x2B07;</div>
    <div class="wind-speed" id="wind-speed"></div>
    <div class="wind-label" id="wind-gusts"></div>
  </div>

  <div class="legend">
    <div><strong>Speed</strong></div>
    <div class="legend-bar"></div>
    <div class="legend-labels"><span>0 kn</span><span id="max-speed-label">? kn</span></div>
  </div>

  <script type="module">
    import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps";

    const map = L.map('map', { zoomControl: true });
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors',
      maxZoom: 19,
    }).addTo(map);

    function speedColor(speed, maxSpeed) {
      const t = maxSpeed > 0 ? Math.min(speed / maxSpeed, 1) : 0;
      const colors = [
        [49, 54, 149], [69, 117, 180], [116, 173, 209], [171, 217, 233],
        [254, 224, 144], [253, 174, 97], [244, 109, 67], [215, 48, 39]
      ];
      const idx = t * (colors.length - 1);
      const lo = Math.floor(idx), hi = Math.ceil(idx);
      const f = idx - lo;
      const r = Math.round(colors[lo][0] * (1-f) + colors[hi][0] * f);
      const g = Math.round(colors[lo][1] * (1-f) + colors[hi][1] * f);
      const b = Math.round(colors[lo][2] * (1-f) + colors[hi][2] * f);
      return `rgb(${r},${g},${b})`;
    }

    function renderMap(data) {
      const { track, analysis, activity_date } = data;
      if (!track || track.length < 2) {
        document.getElementById('stats').innerHTML = '<h3>No GPS data</h3>';
        return;
      }

      const maxSpeed = analysis.track_summary.max_speed_knots || 1;
      document.getElementById('max-speed-label').textContent =
        maxSpeed.toFixed(1) + ' kn';

      for (let i = 1; i < track.length; i++) {
        const p0 = track[i-1], p1 = track[i];
        const color = speedColor(p1.speed_knots, maxSpeed);
        L.polyline([[p0.lat, p0.lon], [p1.lat, p1.lon]], {
          color, weight: 4, opacity: 0.85,
        }).addTo(map);
      }

      L.circleMarker([track[0].lat, track[0].lon], {
        radius: 8, fillColor: '#2ecc71', fillOpacity: 1,
        color: '#fff', weight: 2,
      }).addTo(map).bindPopup('Start');

      const last = track[track.length - 1];
      L.circleMarker([last.lat, last.lon], {
        radius: 8, fillColor: '#e74c3c', fillOpacity: 1,
        color: '#fff', weight: 2,
      }).addTo(map).bindPopup('End');

      // Direction arrows along the track
      const step = Math.max(1, Math.floor(track.length / 15));
      for (let i = step; i < track.length - step; i += step) {
        const p = track[i];
        if (p.heading == null) continue;
        const color = speedColor(p.speed_knots, maxSpeed);
        // CSS rotate: heading 0=North, arrow ▲ points up at 0deg
        const icon = L.divIcon({
          className: '',
          html: `<div style="
            font-size: 14px;
            color: ${color};
            transform: rotate(${p.heading}deg);
            text-shadow: 0 0 3px rgba(0,0,0,0.6);
            line-height: 1;
          ">&#x25B2;</div>`,
          iconSize: [14, 14],
          iconAnchor: [7, 7],
        });
        L.marker([p.lat, p.lon], { icon, interactive: false }).addTo(map);
      }

      const lats = track.map(p => p.lat);
      const lons = track.map(p => p.lon);
      map.fitBounds([
        [Math.min(...lats), Math.min(...lons)],
        [Math.max(...lats), Math.max(...lons)],
      ], { padding: [40, 40] });

      const s = analysis.track_summary;
      const sa = analysis.sailing_analysis;
      const hr = analysis.heart_rate;
      const storm = analysis.storm_indicators || {};
      document.getElementById('stats').innerHTML = `
        <h3>Sailing ${activity_date}</h3>
        <div><span class="label">Distance</span>
             <span class="value">${s.distance_nm} nm</span></div>
        <div><span class="label">Duration</span>
             <span class="value">${s.duration_minutes} min</span></div>
        <div><span class="label">Avg Speed</span>
             <span class="value">${s.avg_speed_knots} kn</span></div>
        <div><span class="label">Max Speed</span>
             <span class="value">${s.max_speed_knots} kn</span></div>
        <div><span class="label">VMG</span>
             <span class="value">${sa.avg_vmg_knots ?? '\\u2014'} kn</span></div>
        <div><span class="label">Maneuvers</span>
             <span class="value">${sa.maneuvers_detected}</span></div>
        <div><span class="label">Avg HR</span>
             <span class="value">${hr.avg_bpm ?? '\\u2014'} bpm</span></div>
        ${storm.precipitation_mm ? `<div><span class="label">Rain</span>
             <span class="value">${storm.precipitation_mm} mm</span></div>` : ''}
      `;

      const w = analysis.wind_conditions;
      if (w && w.wind_speed_knots != null) {
        document.getElementById('wind-panel').style.display = 'block';
        // wind_direction_deg = where wind comes FROM; using it directly
        // as CSS rotation on a down-arrow makes it point where wind blows TO
        document.getElementById('wind-arrow').style.transform =
          `rotate(${w.wind_direction_deg}deg)`;
        document.getElementById('wind-speed').textContent =
          w.wind_speed_knots.toFixed(1) + ' kn';
        document.getElementById('wind-gusts').textContent =
          w.wind_gusts_knots
            ? 'gusts ' + w.wind_gusts_knots.toFixed(1) + ' kn'
            : '';
      }
    }

    const app = new App({ name: "Sailing Map", version: "1.0.0" });

    app.ontoolresult = ({ content }) => {
      const text = content?.find(c => c.type === 'text');
      if (text) {
        try {
          renderMap(JSON.parse(text.text));
        } catch (e) {
          document.getElementById('stats').innerHTML =
            '<h3>Error parsing data</h3>';
        }
      }
    };

    await app.connect();
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()
