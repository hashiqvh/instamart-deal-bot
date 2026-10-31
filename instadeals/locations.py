"""Read explicit place coordinates from full Google Maps URLs without fetching pages."""
import math
import re
from urllib.parse import unquote, urlparse, parse_qs


def maps_location(text):
    parsed = urlparse(text.strip())
    if parsed.scheme != 'https' or parsed.hostname not in {'google.com','www.google.com','maps.google.com'} or parsed.username or parsed.password:
        return None
    path = unquote(parsed.path)
    if not path.startswith('/maps') and parsed.hostname != 'maps.google.com':
        return None
    # Place marker coordinates take precedence over the viewport's @ coordinates.
    match = re.search(r'!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)',path)
    if match:
        lat,lon=map(float,match.groups())
    else:
        query=parse_qs(parsed.query)
        value=(query.get('query') or query.get('q') or [''])[0]
        match=re.fullmatch(r'\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*',value)
        if not match:
            return None  # @ may be only a viewport, not the delivery address.
        lat,lon=map(float,match.groups())
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180:
        return None
    name_match=re.search(r'/place/([^/]+)',path)
    name=name_match.group(1).replace('+',' ')[:60] if name_match else 'Maps location'
    return name,round(lat,6),round(lon,6)
