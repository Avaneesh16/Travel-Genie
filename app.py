from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI
import os
from dotenv import load_dotenv
import secrets
from google_calendar import (
    parse_event_details, create_event, SCOPES, 
    get_events, get_events_at_time, check_overlapping_events,
    format_event, is_valid_event, parse_iso_time,
    check_availability
)
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from collections import defaultdict
import datetime
import pytz
from airtable import Airtable
import json
import requests  # Add this import
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.pool import QueuePool
from sqlalchemy import create_engine
import time
from contextlib import contextmanager
from markupsafe import Markup  # Replace jinja2.Markup with markupsafe.Markup

load_dotenv()

app = Flask(__name__, static_url_path='/static', static_folder='static')
app.secret_key = os.getenv('FLASK_SECRET_KEY')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///chat.db?timeout=30'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'max_overflow': 2,
    'pool_timeout': 30,
    'pool_recycle': 1800,
    'pool_pre_ping': True,
    'connect_args': {
        'timeout': 30,
        'check_same_thread': False
    }
}

db = SQLAlchemy(app)

@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    session = db.session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(32), nullable=False)
    role = db.Column(db.String(10), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=db.func.current_timestamp())

client = OpenAI(
    api_key=os.getenv('OPENAI_API_KEY'),
    # Remove base_url since we're using OpenAI directly
)

# Airtable configuration
AIRTABLE_ENABLED = bool(os.getenv('AIRTABLE_ENABLED', 'false').lower() == 'true')
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID')
AIRTABLE_API_KEY = os.getenv('AIRTABLE_API_KEY')
USER_TABLE_NAME = 'UserProfiles'

airtable = None
if AIRTABLE_ENABLED and AIRTABLE_BASE_ID and AIRTABLE_API_KEY:
    try:
        airtable = Airtable(
            AIRTABLE_BASE_ID,
            USER_TABLE_NAME,
            api_key=AIRTABLE_API_KEY
        )
        # Verify connection and table existence
        test = airtable.get_all(maxRecords=1)
        print("Successfully connected to Airtable")
    except Exception as e:
        print(f"Failed to initialize Airtable: {str(e)}")
        print("Please check:")
        print("1. Your Personal Access Token is correct and starts with 'pat.'")
        print("2. The base ID is correct")
        print("3. A table named 'UserProfiles' exists in your base")
        print("4. The token has proper permissions for the base")
        airtable = None

# Add Voiceflow configuration after existing configurations
VOICEFLOW_API_KEY = os.getenv('VOICEFLOW_API_KEY')
VOICEFLOW_VERSION_ID = os.getenv('VOICEFLOW_VERSION_ID')
VOICEFLOW_API_URL = 'https://general-runtime.voiceflow.com/state/user'

def get_voiceflow_response(user_id, message):
    if not VOICEFLOW_API_KEY:
        return None
        
    try:
        headers = {
            'Authorization': VOICEFLOW_API_KEY,
            'Content-Type': 'application/json'
        }
        
        data = {
            'action': {
                'type': 'text',
                'payload': message
            }
        }
        
        response = requests.post(
            f'{VOICEFLOW_API_URL}/{user_id}/{VOICEFLOW_VERSION_ID}/interact',
            headers=headers,
            json=data
        )
        
        if response.status_code == 200:
            responses = response.json()
            text_responses = []
            
            for trace in responses:
                if trace.get('type') == 'speak' or trace.get('type') == 'text':
                    text_responses.append(trace.get('payload', {}).get('message', ''))
            
            return ' '.join(text_responses) if text_responses else None
        return None
    except Exception as e:
        print(f"Voiceflow error: {str(e)}")
        return None

def validate_preferences(preferences_str):
    try:
        if not preferences_str:
            return get_default_preferences()
        prefs = json.loads(preferences_str)
        return {
            'theme': prefs.get('theme', 'light'),
            'language': prefs.get('language', 'en'),
            'notifications': prefs.get('notifications', True),
            'calendar_default_duration': prefs.get('calendar_default_duration', 60),
            'timezone': prefs.get('timezone', 'America/Denver'),
            'food_preferences': {
                'budget_per_meal': prefs.get('food_preferences', {}).get('budget_per_meal', 20),
                'dietary_restrictions': prefs.get('food_preferences', {}).get('dietary_restrictions', []),
                'cuisine_preferences': prefs.get('food_preferences', {}).get('cuisine_preferences', [])
            },
            'travel_preferences': {
                'mode': prefs.get('travel_preferences', {}).get('mode', 'driving'),
                'max_travel_time': prefs.get('travel_preferences', {}).get('max_travel_time', 60),
                'accommodation_budget': prefs.get('travel_preferences', {}).get('accommodation_budget', 150),
                'preferred_airlines': prefs.get('travel_preferences', {}).get('preferred_airlines', [])
            }
        }
    except json.JSONDecodeError:
        return get_default_preferences()

def get_default_preferences():
    return {
        'theme': 'light',
        'language': 'en',
        'notifications': True,
        'calendar_default_duration': 60,
        'timezone': 'America/Denver',
        'food_preferences': {
            'budget_per_meal': 20,
            'dietary_restrictions': [],
            'cuisine_preferences': []
        },
        'travel_preferences': {
            'mode': 'driving',
            'max_travel_time': 60,
            'accommodation_budget': 150,
            'preferred_airlines': []
        }
    }

def get_user_profile(session_id):
    if not airtable:
        return {'SessionID': session_id, 'Preferences': json.dumps(get_default_preferences())}
    try:
        # Use formula instead of search
        formula = f"{{SessionID}} = '{session_id}'"
        records = airtable.get_all(formula=formula)
        
        if records:
            print(f"Found existing profile for session {session_id}")
            return records[0]['fields']
        else:
            print(f"Creating new profile for session {session_id}")
            # Create default profile
            profile_data = {
                'SessionID': session_id,
                'Preferences': json.dumps(get_default_preferences())
            }
            result = airtable.insert(profile_data)
            return result['fields'] if result else profile_data
    except Exception as e:
        print(f"Error in get_user_profile: {str(e)}")
        return {'SessionID': session_id, 'Preferences': json.dumps(get_default_preferences())}

def create_or_update_user_profile(session_id, profile_data):
    if not airtable:
        return
    try:
        # Search for existing profile
        formula = f"{{SessionID}} = '{session_id}'"
        records = airtable.get_all(formula=formula)
        
        if records:
            record_id = records[0]['id']
            airtable.update(record_id, profile_data)
            print(f"Updated profile for session {session_id}")
        else:
            # Create new profile
            profile_data['SessionID'] = session_id
            airtable.insert(profile_data)
            print(f"Created new profile for session {session_id}")
        return True
    except Exception as e:
        print(f"Error in create_or_update_user_profile: {str(e)}")
        return False

@app.route('/')
def home():
    if 'session_id' not in session:
        session['session_id'] = secrets.token_hex(16)
    
    # Get user profile from Airtable with error handling
    user_profile = get_user_profile(session['session_id'])
    
    # Get chat history from database
    messages = ChatMessage.query.filter_by(session_id=session['session_id']).all()
    return render_template('chat.html', messages=messages, user_profile=user_profile)

@app.route('/chat', methods=['POST'])
def chat():
    user_message = request.form['message']
    session_id = session['session_id']
    
    try:
        with session_scope() as db_session:
            # Save user message
            user_msg = ChatMessage(role='user', content=user_message, session_id=session_id)
            db_session.add(user_msg)
            
            # Check for calendar event
            event_data = parse_event_details(user_message)
            if event_data:
                if not os.path.exists('token.json'):
                    return {'error': 'Please authenticate first'}, 401
                
                try:
                    if event_data["type"] == "view":
                        events = get_events()
                        ai_response = format_calendar_view(events)
                    
                    elif event_data["type"] == "check_availability":
                        events = get_events()
                        availability = check_availability(event_data["date"], events)
                        ai_response = format_availability_response(availability)
                    
                    elif event_data["type"] == "recurring":
                        ai_response = handle_recurring_event(event_data)
                    
                    elif event_data["type"] == "trip_planning":
                        ai_response = handle_trip_planning(event_data, session_id)
                    
                    else:
                        # Handle regular event creation
                        if not event_data.get("summary") or not event_data.get("start_time"):
                            return {'error': 'Could not parse event details. Try "Add [event] at [time]"'}, 400
                        
                        ai_response = create_event(
                            summary=event_data.get("summary"),
                            start_time=event_data.get("start_time"),
                            end_time=event_data.get("end_time"),
                            description=event_data.get("description", ""),
                            location=event_data.get("location", "")
                        )
                
                except Exception as e:
                    return {'error': f"Calendar error: {str(e)}"}, 500
            else:
                # Handle non-calendar messages with OpenAI
                ai_response = handle_chat_message(session_id, user_message)
            
            # Save AI response
            ai_msg = ChatMessage(role='assistant', content=ai_response, session_id=session_id)
            db_session.add(ai_msg)
            
            return {'response': ai_response}
            
    except Exception as e:
        print(f"Error in chat route: {str(e)}")
        return {'error': str(e)}, 500

# Add these helper functions
def format_availability_response(availability):
    if availability['is_available']:
        return f"📅 You're completely free on {availability['date']}! Perfect time for a trip! 🎉"
    busy_times = "\n".join(
        [f"- {start.strftime('%I:%M %p')} to {end.strftime('%I:%M %p')}" 
         for start, end in availability['busy_periods']]
    )
    return (
        f"📅 On {availability['date']}, you have commitments during:\n"
        f"{busy_times}\n\n"
        f"Free time available between these slots!"
    )

def handle_recurring_event(event_data):
    events = get_events()
    conflicts = []
    current_day = event_data["start_date"]
    
    while current_day <= event_data["end_date"]:
        if event_data["is_all_day"]:
            start_time = datetime.datetime.combine(current_day, datetime.time(0,0))
            end_time = start_time + datetime.timedelta(days=1)
        else:
            start_time = datetime.datetime.combine(current_day, event_data["base_time"].time())
            end_time = start_time + datetime.timedelta(hours=1)
        
        daily_conflicts = get_events_at_time(
            start_time.astimezone(pytz.timezone('America/Denver')),
            end_time.astimezone(pytz.timezone('America/Denver'))
        )
        if daily_conflicts:
            conflicts.append({
                "date": current_day.strftime("%Y-%m-%d"),
                "events": daily_conflicts
            })
        
        current_day += datetime.timedelta(days=1)
    
    if conflicts:
        conflict_msg = "🚫 Conflicts found:\n"
        for conflict in conflicts:
            conflict_msg += f"\n📅 {conflict['date']}:\n"
            conflict_msg += "\n".join([f"- {format_event(e)}" for e in conflict['events']])
        return conflict_msg + "\n\nPlease resolve conflicts first!"
    
    # Create events if no conflicts
    created_count = create_recurring_events(event_data)
    return f"✅ Added {created_count} events for {event_data['duration']}!"

def create_recurring_events(event_data):
    """Create recurring events based on event data"""
    created_count = 0
    current_day = event_data["start_date"]
    
    while current_day <= event_data["end_date"]:
        if event_data["is_all_day"]:
            start_time = datetime.datetime.combine(current_day, datetime.time(0,0))
            end_time = start_time + datetime.timedelta(days=1)
        else:
            start_time = datetime.datetime.combine(current_day, event_data["base_time"].time())
            end_time = start_time + datetime.timedelta(hours=1)
        
        create_event(
            summary=event_data.get("summary"),
            start_time=start_time,
            end_time=end_time,
            description=event_data.get("description", ""),
            location=event_data.get("location", "")
        )
        created_count += 1
        current_day += datetime.timedelta(days=event_data["interval"])
    
    return created_count
    events = get_events()
    conflicts = []
    current_day = event_data["start_date"]
    
    while current_day <= event_data["end_date"]:
        if event_data["is_all_day"]:
            start_time = datetime.datetime.combine(current_day, datetime.time(0,0))
            end_time = start_time + datetime.timedelta(days=1)
        else:
            start_time = datetime.datetime.combine(current_day, event_data["base_time"].time())
            end_time = start_time + datetime.timedelta(hours=1)
        
        daily_conflicts = get_events_at_time(
            start_time.astimezone(pytz.timezone('America/Denver')),
            end_time.astimezone(pytz.timezone('America/Denver'))
        )
        if daily_conflicts:
            conflicts.append({
                "date": current_day.strftime("%Y-%m-%d"),
                "events": daily_conflicts
            })
        
        current_day += datetime.timedelta(days=1)
    
    if conflicts:
        conflict_msg = "🚫 Conflicts found:\n"
        for conflict in conflicts:
            conflict_msg += f"\n📅 {conflict['date']}:\n"
            conflict_msg += "\n".join([f"- {format_event(e)}" for e in conflict['events']])
        return conflict_msg + "\n\nPlease resolve conflicts first!"
    
    # Create events if no conflicts
    created_count = create_recurring_events(event_data)
    return f"✅ Added {created_count} events for {event_data['duration']}!"

def handle_trip_planning(event_data, session_id):
    """Enhanced trip planning with preferences"""
    # Get user preferences from Airtable
    user_profile = get_user_profile(session_id)
    preferences = json.loads(user_profile.get('Preferences', '{}'))
    travel_prefs = preferences.get('travel_preferences', {})
    
    # Get calendar availability
    free_days = []
    current_day = event_data["start_date"]
    end_date = event_data.get("end_date") or (current_day + datetime.timedelta(days=7))
    
    # Check calendar availability
    while current_day <= end_date:
        availability = check_availability(current_day, get_events())
        if availability['is_available']:
            free_days.append({
                'date': current_day,
                'formatted_date': current_day.strftime("%A, %B %d")
            })
        current_day += datetime.timedelta(days=1)
    
    if not free_days:
        return "🚫 No available dates found in your calendar for this trip."
    
    # Format travel plan based on preferences
    response = format_travel_plan(
        destination=event_data['location'],
        free_days=free_days,
        preferences=travel_prefs
    )
    
    # Create calendar events for the trip
    try:
        created_events = create_trip_events(free_days[0]['date'], travel_prefs)
        if created_events:
            response += "\n\n✅ Added trip to your calendar!"
    except Exception as e:
        response += f"\n\n⚠️ Note: Couldn't add to calendar: {str(e)}"
    
    return response

def format_travel_plan(destination, free_days, preferences):
    """Format travel plan with bullet points"""
    mode = preferences.get('mode', 'flying')
    budget = preferences.get('accommodation_budget', 150)
    max_time = preferences.get('max_travel_time', 60)
    airlines = preferences.get('preferred_airlines', [])
    
    # Build response in parts for better structure
    header = f"🌍 Trip to {destination}\n"
    
    dates_section = "\n📅 Available Dates:\n" + "\n".join(
        f"• {day['formatted_date']}" 
        for day in free_days[:3]
    )
    
    prefs_section = "\n\n🎯 Your Preferences:\n"
    prefs_section += f"• Mode: {mode.title()}\n"
    prefs_section += f"• Budget: ${budget} per night\n"
    prefs_section += f"• Max travel time: {max_time} minutes"
    
    if mode == "flying" and airlines:
        prefs_section += f"\n• Preferred airlines: {', '.join(airlines)}"
    
    return header + dates_section + prefs_section

def create_trip_events(start_date, preferences):
    """Create calendar events for the trip"""
    events = []
    
    # Main trip event
    main_event = create_event(
        summary="Trip",
        start_time=start_date,
        is_all_day=True,
        description=f"Travel Mode: {preferences.get('mode', 'flying')}\n" +
                   f"Budget: ${preferences.get('accommodation_budget', 150)}/night"
    )
    events.append(main_event)
    
    # Add travel time buffers based on preferences
    if preferences.get('mode') == 'flying':
        # Add airport buffer times
        buffer_before = create_event(
            summary="Travel to Airport",
            start_time=start_date - datetime.timedelta(hours=3),
            is_all_day=False
        )
        events.append(buffer_before)
    
    return events

def handle_chat_message(session_id, user_message):
    """Enhanced chat handling with travel detection"""
    # Check if it's a travel-related query
    travel_keywords = ['trip to', 'travel to', 'visit', 'vacation in', 'planning to go to']
    is_travel_query = any(keyword in user_message.lower() for keyword in travel_keywords)
    
    if is_travel_query:
        # Extract destination from message
        destination = extract_destination(user_message)
        if destination:
            # Create event data for trip planning
            event_data = {
                "type": "trip_planning",
                "location": destination,
                "start_date": datetime.datetime.now() + datetime.timedelta(days=1),
                "end_date": datetime.datetime.now() + datetime.timedelta(days=30)
            }
            return handle_trip_planning(event_data, session_id)
    
    # If not a travel query, proceed with normal chat
    history = ChatMessage.query.filter_by(session_id=session_id).all()
    messages = [{'role': msg.role, 'content': msg.content} for msg in history]
    
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",  # Use OpenAI's model
        messages=[
            {
                "role": "system",
                "content": "You are Genie, a helpful AI assistant focused on travel planning and calendar management. Help users plan trips and manage their schedule effectively."
            },
            *[{'role': msg.role, 'content': msg.content} for msg in history],
            {"role": "user", "content": user_message}
        ],
        stream=False
    )
    
    return response.choices[0].message.content

def extract_destination(message):
    """Extract destination from travel query"""
    keywords = ['trip to', 'travel to', 'visit', 'vacation in', 'planning to go to']
    for keyword in keywords:
        if keyword in message.lower():
            # Extract text after keyword
            parts = message.lower().split(keyword)
            if len(parts) > 1:
                # Clean up the destination text
                destination = parts[1].strip().split()[0].title()
                return destination
    return None

def format_calendar_view(events):
    """Format calendar events in a cleaner way"""
    if not events:
        return "🎉 Your calendar is clear!"
    
    response = "📅 Your Calendar\n\n"
    grouped = group_events_by_date(events)
    
    for date_str, daily_events in sorted(grouped.items()):
        date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        if not daily_events:
            continue
            
        response += f"<div class='calendar-date'>\n"
        response += f"<h3>{date_obj.strftime('%A, %B %d, %Y')}</h3>\n"
        
        for event in daily_events:
            if is_valid_event(event):
                if 'date' in event['start']:  # All-day event
                    response += (
                        "<div class='calendar-event all-day'>\n"
                        "<span class='calendar-icon'>📅</span>\n"
                        "<span class='event-title'>"
                        f"<strong>{event['summary']}</strong> (All day)"
                        "</span>\n"
                        "</div>\n"
                    )
                else:  # Timed event
                    start = parse_iso_time(event['start']['dateTime'])
                    end = parse_iso_time(event['end']['dateTime'])
                    response += (
                        "<div class='calendar-event'>\n"
                        "<span class='calendar-icon'>🕒</span>\n"
                        "<span class='event-time'>"
                        f"{start.strftime('%I:%M %p')} - {end.strftime('%I:%M %p')}"
                        "</span>\n"
                        f"<span class='event-title'><strong>{event['summary']}</strong></span>\n"
                        "</div>\n"
                    )
        response += "</div>\n"
    
    return response

@app.route('/authorize')
def authorize():
    flow = InstalledAppFlow.from_client_secrets_file(
        'credentials.json',
        scopes=SCOPES,
        redirect_uri=url_for('oauth_callback', _external=True)
    )
    authorization_url, _ = flow.authorization_url(
        prompt='consent',
        login_hint='avushetti@gmail.com'
    )
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth_callback():
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            'credentials.json',
            scopes=SCOPES,
            redirect_uri=url_for('oauth_callback', _external=True)
        )
        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials
        
        with open('token.json', 'w') as token:
            token.write(credentials.to_json())
        
        return redirect(url_for('home'))
    except Exception as e:
        return f"Authentication failed: {str(e)}", 400

@app.route('/clear', methods=['POST'])
def clear_history():
    session_id = session.get('session_id')
    if session_id:
        max_retries = 3
        retry_delay = 0.1  # seconds
        
        for attempt in range(max_retries):
            try:
                with session_scope() as db_session:
                    db_session.query(ChatMessage).filter_by(session_id=session_id).delete()
                return '', 204
            except Exception as e:
                if attempt == max_retries - 1:  # Last attempt
                    print(f"Failed to clear history after {max_retries} attempts: {str(e)}")
                    return 'Database error', 500
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
    return '', 204

@app.route('/update_profile', methods=['POST'])
def update_profile():
    if not airtable:
        return jsonify({
            'success': False,
            'error': 'Airtable is not configured'
        }), 400
        
    session_id = session['session_id']
    preferences_str = request.form.get('preferences', '{}')
    
    try:
        # Validate and structure the preferences
        validated_prefs = validate_preferences(preferences_str)
        
        # Update user profile in Airtable
        profile_data = {
            'Preferences': json.dumps(validated_prefs)
        }
        
        if create_or_update_user_profile(session_id, profile_data):
            return jsonify({
                'success': True,
                'message': 'Preferences updated successfully',
                'preferences': validated_prefs
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to update preferences'
            }), 500
    except Exception as e:
        print(f"Error in update_profile: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400

@app.route('/signin')
def signin():
    # Redirect to home if already logged in
    if 'user_email' in session:
        return redirect(url_for('home'))
    return render_template('signin.html')

@app.route('/logout')
def logout():
    try:
        if 'user_email' in session:
            # Update Airtable session
            signin_table = Airtable(
                os.getenv('AIRTABLE_BASE_ID'),
                'SigninInfo',
                os.getenv('AIRTABLE_API_KEY')
            )
            formula = f"{{Email}} = '{session['user_email']}'"
            users = signin_table.get_all(formula=formula)
            if users:
                signin_table.update(users[0]['id'], {'Session': ''})
    except Exception as e:
        print(f"Error updating Airtable session on logout: {str(e)}")
    finally:
        # Always clear session data, even if Airtable update fails
        session.clear()
        
    # Use the correct route name
    return redirect('/signin')

@app.route('/auth', methods=['POST'])
def auth():
    try:
        data = request.json
        signin_table = Airtable(
            os.getenv('AIRTABLE_BASE_ID'),
            'SigninInfo',
            os.getenv('AIRTABLE_API_KEY')
        )
        
        if data['type'] == 'signup':
            # Check if email already exists
            formula = f"{{Email}} = '{data['email']}'"
            existing_user = signin_table.get_all(formula=formula)
            
            if existing_user:
                return jsonify({
                    'success': False,
                    'error': 'Email already registered'
                }), 400
            
            # Create new user with hashed password
            session_id = secrets.token_hex(16)
            hashed_password = generate_password_hash(data['password'], method='pbkdf2:sha256')
            
            signin_table.insert({
                'Name': data['name'],
                'Email': data['email'],
                'Password': hashed_password,
                'Session': session_id
            })
            
            # Set session data for new user
            session['user_email'] = data['email']
            session['user_name'] = data['name']
            session['session_id'] = session_id
            
        else:  # signin
            formula = f"{{Email}} = '{data['email']}'"
            users = signin_table.get_all(formula=formula)
            
            if not users:
                return jsonify({
                    'success': False,
                    'error': 'Email not found'
                }), 401
            
            user = users[0]
            if not check_password_hash(user['fields']['Password'], data['password']):
                return jsonify({
                    'success': False,
                    'error': 'Invalid password'
                }), 401
            
            # Update session
            session_id = secrets.token_hex(16)
            signin_table.update(user['id'], {'Session': session_id})
            
            # Set session data
            session['user_email'] = data['email']
            session['user_name'] = user['fields'].get('Name')
            session['session_id'] = session_id
        
        return jsonify({'success': True})
        
    except Exception as e:
        print(f"Auth error: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

def group_events_by_date(events):
    grouped = defaultdict(list)
    for event in events:
        if not is_valid_event(event):
            continue
        start = event['start'].get('dateTime', event['start'].get('date'))
        date_str = parse_iso_time(start).strftime("%Y-%m-%d")
        grouped[date_str].append(event)
    
    # Sort dates and filter empty days
    return {
        k: v for k, v in sorted(
            grouped.items(),
            key=lambda x: x[0]
        ) if v
    }

@app.template_filter('process_calendar')
def process_calendar(text):
    """Convert calendar markdown to HTML with proper styling"""
    return Markup(text)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)