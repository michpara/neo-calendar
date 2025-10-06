import os
import json
import pytz
import requests
from datetime import datetime, timedelta, timezone
from icalendar import Calendar, Event
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# === Load Config from Environment ===
API_KEY = os.getenv("NASA_API_KEY")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

if not API_KEY or not GOOGLE_CALENDAR_ID:
    raise EnvironmentError("Missing required environment variables: NASA_API_KEY or GOOGLE_CALENDAR_ID")

# === Date Range ===
today = datetime.now(timezone.utc).date()
seven_days_later = today + timedelta(days=7)

params = {
    "start_date": today.isoformat(),
    "end_date": seven_days_later.isoformat(),
    "api_key": API_KEY
}

# === Initialize Local ICS Calendar ===
local_calendar = Calendar()
local_calendar.add("prodid", "-//Near Earth Asteroid Tracker//example.com//")
local_calendar.add("version", "2.0")

# === Google Calendar Auth (using token JSON from env variable) ===
def get_google_calendar_service():
    creds = None
    token_json_str = os.getenv("GOOGLE_TOKEN_JSON")

    if not token_json_str:
        raise RuntimeError("Missing GOOGLE_TOKEN_JSON environment variable")

    creds = Credentials.from_authorized_user_info(json.loads(token_json_str), SCOPES)

    # Refresh token if expired
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return build("calendar", "v3", credentials=creds)

# === Delete All Events from Google Calendar ===
def delete_all_events(service, calendar_id=GOOGLE_CALENDAR_ID):
    print("🗑️ Deleting all existing events...")
    page_token = None
    while True:
        events_result = service.events().list(
            calendarId=calendar_id, pageToken=page_token).execute()
        events = events_result.get('items', [])
        if not events:
            break
        for event in events:
            try:
                service.events().delete(calendarId=calendar_id, eventId=event['id']).execute()
                print(f"Deleted event: {event.get('summary', '(no title)')}")
            except Exception as e:
                print(f"⚠️ Could not delete event: {e}")
        page_token = events_result.get('nextPageToken')
        if not page_token:
            break

# === Fetch Near-Earth Object Data ===
try:
    response = requests.get("https://api.nasa.gov/neo/rest/v1/feed", params=params)
    response.raise_for_status()
    neo_data = response.json()
except Exception as e:
    print(f"❌ Failed to fetch NEO data: {e}")
    exit(1)

# === Main Script ===
try:
    calendar_service = get_google_calendar_service()
    delete_all_events(calendar_service, GOOGLE_CALENDAR_ID)
except Exception as e:
    print(f"❌ Google Calendar authentication or deletion error: {e}")
    exit(1)

for date_key in sorted(neo_data.get("near_earth_objects", {})):
    for asteroid in neo_data["near_earth_objects"][date_key]:
        try:
            approach_info = asteroid.get("close_approach_data", [])
            if not approach_info or not asteroid["is_sentry_object"]:
                continue

            date_full = approach_info[0].get("close_approach_date_full", "")
            if " " not in date_full:
                continue  # Skip if time not provided

            date_part, time_part = date_full.split()
            dt = datetime.strptime(f"{date_part} {time_part}", "%Y-%b-%d %H:%M").replace(tzinfo=pytz.utc)

            rounded_minute = (dt.minute // 5) * 5
            start_time = dt.replace(minute=rounded_minute, second=0)
            end_time = start_time + timedelta(minutes=5)

            diameter_min = float(asteroid["estimated_diameter"]["kilometers"]["estimated_diameter_min"])
            diameter_max = float(asteroid["estimated_diameter"]["kilometers"]["estimated_diameter_max"])
            is_hazardous = asteroid["is_potentially_hazardous_asteroid"]
            velocity_kms = float(approach_info[0]["relative_velocity"]["kilometers_per_second"])
            miss_distance_km = float(approach_info[0]["miss_distance"]["kilometers"])
            orbiting_body = approach_info[0]["orbiting_body"]
            abs_magnitude = asteroid.get("absolute_magnitude_h", "N/A")
            jpl_url = asteroid.get("nasa_jpl_url", "")

            diameter_m = diameter_min * 1000
            velocity_kmh = velocity_kms * 3600
            moon_distance_km = 384_400
            distance_from_moon = miss_distance_km / moon_distance_km

            hazard_msg = "This asteroid **IS** hazardous." if is_hazardous else "This asteroid is **NOT** hazardous."
            football_field_m = 91.44
            length_in_fields = diameter_m / football_field_m
            jet_speed_kmh = 900
            speed_vs_jet = velocity_kmh / jet_speed_kmh

            description = (
                f"{hazard_msg}\n\n"
                f"Diameter: {diameter_min:.2f}–{diameter_max:.2f} km (~{diameter_m:.0f} meters, "
                f"~{length_in_fields:.1f} football fields)\n\n"
                f"Velocity: {velocity_kms:.2f} km/s (~{velocity_kmh:,.0f} km/h, ~{speed_vs_jet:.1f}x jet speed)\n\n"
                f"Distance: {miss_distance_km:,.0f} km (~{distance_from_moon:.1f}x Moon distance)\n\n"
                f"Absolute Magnitude (H): {abs_magnitude}\n"
                f"Orbiting Body: {orbiting_body}\n\n"
                f"This asteroid poses no risk during this pass.\n"
            )

            if jpl_url:
                description += f"\nMore Info: {jpl_url}"

            # Add to local ICS calendar
            ics_event = Event()
            ics_event.add("summary", asteroid["name"])
            ics_event.add("dtstart", start_time)
            ics_event.add("dtend", end_time)
            ics_event.add("description", description)
            local_calendar.add_component(ics_event)

            # Upload event to Google Calendar
            gcal_event = {
                "summary": asteroid["name"],
                "description": description,
                "start": {
                    "dateTime": start_time.isoformat(),
                    "timeZone": "UTC"
                },
                "end": {
                    "dateTime": end_time.isoformat(),
                    "timeZone": "UTC"
                }
            }

            created_event = calendar_service.events().insert(
                calendarId=GOOGLE_CALENDAR_ID, body=gcal_event
            ).execute()

            print(f"✅ Event created: {created_event.get('htmlLink')}")

        except Exception as e:
            print(f"⚠️ Skipped asteroid due to error: {e}")

# Save the local ICS calendar file
with open("calendar.ics", "wb") as f:
    f.write(local_calendar.to_ical())
    print("📁 Saved local ICS calendar as 'calendar.ics'")
