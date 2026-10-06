import re

HOME = "Home"
SEARCH = "Search"
POST_LISTING = "Post"
MY_BOOKINGS = "My bookings"
DELETE = "Delete"
PAYMENTS = "Payments"
SETTINGS = "Settings"
PROFILE = "Profile"
HELP = "Help"
RENT = "I want to rent"
HOST = "I want to host"

MENU_LABELS = [HOME, SEARCH, POST_LISTING, MY_BOOKINGS, DELETE, PAYMENTS, SETTINGS, PROFILE, HELP, RENT, HOST]
MENU_REGEX = "^(" + "|".join(re.escape(label) for label in MENU_LABELS) + ")$"
