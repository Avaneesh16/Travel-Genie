import os
from airtable import Airtable
import json
from typing import Optional, Dict, Any

class AirtableManager:
    def __init__(self, base_id: str, api_key: str, table_name: str = 'UserProfiles'):
        self.is_enabled = bool(base_id and api_key)
        self.table_name = table_name
        self.airtable = None
        
        if self.is_enabled:
            try:
                self.airtable = Airtable(base_id, table_name, api_key)
                # Verify connection
                self.airtable.get_all(maxRecords=1)
                print("Successfully connected to Airtable")
            except Exception as e:
                print(f"Failed to initialize Airtable: {str(e)}")
                print("Please check:")
                print("1. Your Personal Access Token is correct and starts with 'pat.'")
                print("2. The base ID is correct")
                print(f"3. A table named '{table_name}' exists in your base")
                print("4. The token has proper permissions for the base")
                self.airtable = None
                self.is_enabled = False

    def get_default_preferences(self) -> dict:
        """Return default user preferences"""
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

    def validate_preferences(self, preferences_str: str) -> dict:
        """Validate and structure user preferences"""
        try:
            if not preferences_str:
                return self.get_default_preferences()
            
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
            return self.get_default_preferences()

    def get_user_profile(self, session_id: str) -> Dict[str, Any]:
        """Get user profile from Airtable"""
        if not self.is_enabled:
            return {
                'SessionID': session_id,
                'Preferences': json.dumps(self.get_default_preferences())
            }
        
        try:
            formula = f"{{SessionID}} = '{session_id}'"
            records = self.airtable.get_all(formula=formula)
            
            if records:
                print(f"Found existing profile for session {session_id}")
                return records[0]['fields']
            else:
                print(f"Creating new profile for session {session_id}")
                profile_data = {
                    'SessionID': session_id,
                    'Preferences': json.dumps(self.get_default_preferences())
                }
                result = self.airtable.insert(profile_data)
                return result['fields'] if result else profile_data
                
        except Exception as e:
            print(f"Error in get_user_profile: {str(e)}")
            return {
                'SessionID': session_id,
                'Preferences': json.dumps(self.get_default_preferences())
            }

    def update_user_profile(self, session_id: str, profile_data: Dict[str, Any]) -> bool:
        """Update or create user profile in Airtable"""
        if not self.is_enabled:
            return False
            
        try:
            formula = f"{{SessionID}} = '{session_id}'"
            records = self.airtable.get_all(formula=formula)
            
            if records:
                record_id = records[0]['id']
                self.airtable.update(record_id, profile_data)
                print(f"Updated profile for session {session_id}")
            else:
                profile_data['SessionID'] = session_id
                self.airtable.insert(profile_data)
                print(f"Created new profile for session {session_id}")
            return True
            
        except Exception as e:
            print(f"Error in update_user_profile: {str(e)}")
            return False

    def format_preferences_display(self, preferences_str: str) -> str:
        """Format user preferences for display in chat"""
        try:
            prefs = json.loads(preferences_str)
            if not prefs:
                return "No preferences set yet. You can set them using the preferences panel on the left."
            
            response = "🎯 Here are your current preferences:\n\n"
            
            # General Settings
            response += "⚙️ General Settings\n"
            response += f"• Theme: {prefs.get('theme', 'light').title()}\n"
            response += f"• Language: {prefs.get('language', 'en').upper()}\n"
            response += f"• Timezone: {prefs.get('timezone', 'America/Denver')}\n\n"
            
            # Food Preferences
            food_prefs = prefs.get('food_preferences', {})
            response += "🍽️ Food Preferences\n"
            response += f"• Budget per meal: ${food_prefs.get('budget_per_meal', 20)}\n"
            
            dietary = food_prefs.get('dietary_restrictions', [])
            if dietary:
                response += f"• Dietary restrictions: {', '.join(dietary)}\n"
                
            cuisine = food_prefs.get('cuisine_preferences', [])
            if cuisine:
                response += f"• Favorite cuisines: {', '.join(cuisine)}\n"
            response += "\n"
            
            # Travel Preferences
            travel_prefs = prefs.get('travel_preferences', {})
            response += "✈️ Travel Preferences\n"
            response += f"• Preferred mode: {travel_prefs.get('mode', 'driving').title()}\n"
            response += f"• Max travel time: {travel_prefs.get('max_travel_time', 60)} minutes\n"
            response += f"• Accommodation budget: ${travel_prefs.get('accommodation_budget', 150)}/night\n"
            
            airlines = travel_prefs.get('preferred_airlines', [])
            if airlines:
                response += f"• Preferred airlines: {', '.join(airlines)}\n\n"
            
            response += "\nTo update these preferences, use the settings panel on the left side of the screen."
            return response
            
        except Exception as e:
            print(f"Error formatting preferences: {str(e)}")
            return "I couldn't retrieve your preferences at the moment. Please try again or check the preferences panel on the left."
