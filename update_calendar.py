# === Imports ===
import os
import pytz
import requests
from datetime import datetime, timedelta, timezone
from icalendar import Calendar, Event
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# === Load Config from Environment ===
API_KEY = os.getenv("NASA_API_KEY")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# === Safety Checks ===
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

# === Google Calendar Auth ===
def get_google_calendar_service():
    """Returns an authenticated Google Calendar service."""
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
                raise FileNotFoundError("Missing Google credentials.json file")

            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save credentials for future use
        with open(TOKEN_FILE, "w") as token_file:
            token_file.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)

# === Delete all events from Google Calendar ===
def delete_all_events(service, calendar_id=GOOGLE_CALENDAR_ID):
    """Deletes all events from the specified Google Calendar."""
    page_token = None
    while True:
        events = service.events().list(calendarId=calendar_id, pageToken=page_token).execute()
        for event in events.get('items', []):
            try:
                service.events().delete(calendarId=calendar_id, eventId=event['id']).execute()
                print(f"Deleted event: {event.get('summary', '(no title)')}")
            except Exception as e:
                print(f"⚠️ Could not delete event: {e}")
        page_token = events.get('nextPageToken')
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

# === Authenticate once ===
calendar_service = get_google_calendar_service()

# === Delete all existing events before adding new ones ===
print("🗑️ Deleting all existing events from Google Calendar...")
delete_all_events(calendar_service)

# === Process Each Asteroid ===
for date_key in sorted(neo_data.get("near_earth_objects", {})):
    for asteroid in neo_data["near_earth_objects"][date_key]:
        try:
            # Skip if no approach data or not a sentry object
            approach_info = asteroid.get("close_approach_data", [])
            if not approach_info or not asteroid["is_sentry_object"]:
                continue

            date_full = approach_info[0].get("close_approach_date_full", "")
            if " " not in date_full:
                continue  # Skip if time is not provided

            # Convert to datetime (UTC)
            date_part, time_part = date_full.split()
            dt = datetime.strptime(f"{date_part} {time_part}", "%Y-%b-%d %H:%M").replace(tzinfo=pytz.utc)

            # Round time to 5-minute blocks
            rounded_minute = (dt.minute // 5) * 5
            start_time = dt.replace(minute=rounded_minute, second=0)
            end_time = start_time + timedelta(minutes=5)

            # Extract asteroid details
            diameter_min = float(asteroid["estimated_diameter"]["kilometers"]["estimated_diameter_min"])
            diameter_max = float(asteroid["estimated_diameter"]["kilometers"]["estimated_diameter_max"])
            is_hazardous = asteroid["is_potentially_hazardous_asteroid"]
            velocity_kms = float(approach_info[0]["relative_velocity"]["kilometers_per_second"])
            miss_distance_km = float(approach_info[0]["miss_distance"]["kilometers"])
            orbiting_body = approach_info[0]["orbiting_body"]
            abs_magnitude = asteroid.get("absolute_magnitude_h", "N/A")
            jpl_url = asteroid.get("nasa_jpl_url", "")

            # Calculations
            diameter_m = diameter_min * 1000
            velocity_kmh = velocity_kms * 3600
            moon_distance_km = 384_400
            distance_from_moon = miss_distance_km / moon_distance_km

            hazard_msg = "This asteroid **IS** hazardous." if is_hazardous else "This asteroid is **NOT** hazardous."
            football_field_m = 91.44
            length_in_fields = diameter_m / football_field_m
            jet_speed_kmh = 900
            speed_vs_jet = velocity_kmh / jet_speed_kmh

            # Description
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

            # === Add to ICS File ===
            ics_event = Event()
            ics_event.add("summary", asteroid["name"])
            ics_event.add("dtstart", start_time)
            ics_event.add("dtend", end_time)
            ics_event.add("description", description)
            local_calendar.add_component(ics_event)

            # === Upload to Google Calendar ===
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

# === Save Local ICS File ===
with open("calendar.ics", "wb") as f:
    f.write(local_calendar.to_ical())
    print("📁 Saved local ICS calendar as 'calendar.ics'")
