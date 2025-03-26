import os
from datetime import datetime, timedelta
import dateparser
import re
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import pytz

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_PATH = "token.json"

def get_calendar_service(credentials_path="credentials.json"):
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open(TOKEN_PATH, "w") as token:
            token.write(creds.to_json())
    
    return build("calendar", "v3", credentials=creds)

def create_event(summary, start_time, is_all_day=False):
    """Create a calendar event"""
    service = get_calendar_service()
    
    # Set end time to 1 hour after start if not all-day
    end_time = start_time + timedelta(hours=1)
    
    event = {
        'summary': summary,
        'start': {
            'dateTime': start_time.isoformat(),
            'timeZone': 'America/Denver',
        },
        'end': {
            'dateTime': end_time.isoformat(),
            'timeZone': 'America/Denver',
        },
    }
    
    if is_all_day:
        event['start'] = {'date': start_time.date().isoformat()}
        event['end'] = {'date': end_time.date().isoformat()}
    
    try:
        created_event = service.events().insert(calendarId='primary', body=event).execute()
        return created_event
    except Exception as e:
        print(f"Failed to create event: {str(e)}")
        return None

def parse_natural_datetime(time_str):
    """Parse natural language date/time expressions"""
    settings = {
        'PREFER_DATES_FROM': 'future',
        'TIMEZONE': 'America/Denver',
        'RETURN_AS_TIMEZONE_AWARE': True,
        'DATE_ORDER': 'MDY'
    }
    
    # Handle special cases
    time_str = time_str.lower()
    if 'noon' in time_str:
        time_str = time_str.replace('noon', '12:00')
    elif 'midnight' in time_str:
        time_str = time_str.replace('midnight', '00:00')
    
    # Parse the date/time
    parsed_date = dateparser.parse(time_str, settings=settings)
    
    # If only time is specified, assume today/tomorrow based on current time
    current_time = datetime.now(pytz.timezone('America/Denver'))
    if parsed_date and parsed_date.date() == current_time.date():
        if parsed_date < current_time:
            parsed_date = parsed_date + timedelta(days=1)
    
    return parsed_date

def parse_event_details(message):
    """Enhanced event parsing with natural language support"""
    try:
        msg_lower = message.lower()
        
        # First check for view/show calendar commands
        view_patterns = [
            r"(?:view|show|display|list|check) (?:my )?(?:calendar|schedule|events|agenda)",
            r"what(?:'s| is) (?:on )?(?:my )?calendar",
            r"what do i have scheduled",
            r"(?:show|tell) me my (?:calendar|schedule|events)"
        ]
        
        # Check if message matches any view pattern
        if any(re.search(pattern, msg_lower) for pattern in view_patterns):
            return {"type": "view"}
        
        # Continue with existing event patterns...
        event_patterns = [
            r"(?:add|create|schedule) ([^\"]+?)(?:on|at|for|tomorrow|today|next|this) (.+)",
            r"(?:add|create|schedule) ([^\"]+?) (?:on|at|for) (.+)",
            r"(?:add|create|schedule) (.+?) at (.+)"
        ]
        
        for pattern in event_patterns:
            match = re.search(pattern, msg_lower)
            if match:
                # Extract summary and time parts
                summary = match.group(1).strip()
                time_str = match.group(2).strip()
                
                # Clean up the summary
                summary = re.sub(r'\b(?:on|at|for|tomorrow|today)\b.*', '', summary).strip()
                
                # Parse the date/time
                start_time = parse_natural_datetime(time_str)
                
                if start_time:
                    return {
                        "type": "create",
                        "summary": summary.title(),
                        "start_time": start_time,
                        "is_all_day": 'all day' in msg_lower
                    }
        
        # Check for trip planning with natural dates
        trip_pattern = r"(?:plan|add) (?:a |an )?trip to (.+?)(?: from| on| for| starting)? (.+?)?(?: to | until | through )(.+)?"
        trip_match = re.search(trip_pattern, msg_lower)
        if trip_match:
            location = trip_match.group(1).strip()
            start_str = trip_match.group(2)
            end_str = trip_match.group(3)
            
            current_time = datetime.now(pytz.timezone('America/Denver'))
            start_date = parse_natural_datetime(start_str) if start_str else (current_time + timedelta(days=1))
            end_date = parse_natural_datetime(end_str) if end_str else (start_date + timedelta(days=7))
            
            return {
                "type": "trip_planning",
                "location": location.title(),
                "start_date": start_date,
                "end_date": end_date,
                "is_all_day": True
            }
        
        # ... rest of existing parsing code ...
        
    except Exception as e:
        print(f"Parse error: {str(e)}")
        return None

def get_events(max_results=10):
    try:
        service = get_calendar_service()
        now = datetime.utcnow().isoformat() + 'Z'
        events_result = service.events().list(
            calendarId='primary',
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except HttpError as error:
        print(f"Error fetching events: {error}")
        return []

def parse_datetime(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        local_tz = pytz.timezone('America/Los_Angeles')  # Update with your timezone
        return dt.astimezone(local_tz).strftime("%a %b %d, %Y at %I:%M %p")
    except:
        return iso_str 

def parse_iso_time(iso_str):
    """Handle timezone-naive and aware datetimes"""
    dt = dateparser.parse(iso_str)
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=pytz.UTC)
    return dt

def check_overlapping_events(events):
    # Filter out placeholder events first
    valid_events = [e for e in events if is_valid_event(e)]
    # Convert events to comparable datetime objects
    parsed_events = []
    for event in valid_events:
        start = event['start'].get('dateTime', event['start'].get('date'))
        end = event['end'].get('dateTime', event['end'].get('date'))
        parsed_events.append({
            'summary': event['summary'],
            'start': parse_iso_time(start),
            'end': parse_iso_time(end)
        })
    
    # Ensure all datetimes are timezone-aware
    utc = pytz.UTC
    for event in parsed_events:
        if event['start'].tzinfo is None:
            event['start'] = event['start'].replace(tzinfo=utc)
        if event['end'].tzinfo is None:
            event['end'] = event['end'].replace(tzinfo=utc)
    
    # Sort by start time
    sorted_events = sorted(parsed_events, key=lambda x: x['start'])
    
    overlaps = []
    for i in range(len(sorted_events)-1):
        current = sorted_events[i]
        next_evt = sorted_events[i+1]
        
        if current['end'] > next_evt['start']:
            overlaps.append(
                f"⏳ **{current['summary']}** (until {current['end'].strftime('%I:%M %p')}) "
                f"clashes with **{next_evt['summary']}** (starts {next_evt['start'].strftime('%I:%M %p')})"
            )
    
    return overlaps 

def is_valid_event(event):
    summary = event.get('summary', '').lower()
    return all(
        bad not in summary 
        for bad in ["to calendar", "add", "create", "schedule", "event"]
    ) and len(summary.strip()) > 3

def format_event(event):
    start = event['start'].get('dateTime', event['start'].get('date'))
    end = event['end'].get('dateTime', event['end'].get('date'))
    
    if 'date' in event['start']:  # All-day event
        date_str = datetime.strptime(start, "%Y-%m-%d").strftime("%b %d, %Y")
        return f"📅 **All Day**\n{date_str}\n**{event['summary']}**"
    
    # Timed event
    start_dt = parse_iso_time(start)
    end_dt = parse_iso_time(end)
    return (
        f"🕒 **{start_dt.astimezone(pytz.timezone('America/Denver')).strftime('%I:%M %p')} - "
        f"{end_dt.astimezone(pytz.timezone('America/Denver')).strftime('%I:%M %p')}**\n"
        f"**{event['summary']}**\n"
        f"_{start_dt.astimezone(pytz.timezone('America/Denver')).strftime('%a, %b %d %Y')}_"
    ) 

def check_availability(target_date, events):
    """Check if user is free on a specific date"""
    mst = pytz.timezone('America/Denver')
    target_day = target_date.astimezone(mst).date()
    
    busy_slots = []
    for event in events:
        start = event['start'].get('dateTime', event['start'].get('date'))
        start_dt = parse_iso_time(start)
        
        if start_dt.astimezone(mst).date() == target_day:
            end = event['end'].get('dateTime', event['end'].get('date'))
            end_dt = parse_iso_time(end)
            busy_slots.append((
                start_dt.astimezone(mst),
                end_dt.astimezone(mst)
            ))
    
    return {
        'date': target_date.astimezone(mst).strftime("%A, %b %d %Y"),
        'is_available': len(busy_slots) == 0,
        'busy_periods': busy_slots
    } 

def get_events_at_time(start_time, end_time):
    """Get events overlapping with a time range"""
    try:
        service = get_calendar_service()
        time_min = start_time.isoformat()
        time_max = end_time.isoformat()
        
        events_result = service.events().list(
            calendarId='primary',
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except HttpError as error:
        print(f"Error checking conflicts: {error}")
        return []