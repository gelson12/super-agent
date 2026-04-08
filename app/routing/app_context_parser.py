"""
APP_CONTEXT Parser — reads app-injected control metadata from mobile requests.

The mobile app injects a structured block into the message body:

    [APP_CONTEXT]
    REQUEST_CATEGORY=LOCATION
    ROUTE_TO=GEMINI_ONLY
    LOCATION_PERMISSION=GRANTED
    DEVICE_LOCATION_SOURCE=GOOGLE_PHONE_API
    CURRENT_LAT=51.5074
    CURRENT_LON=-0.1278
    LOCATION_ACCURACY=10.5
    VOICE_MODE=false
    USER_QUERY=Where is the nearest pharmacy?
    [/APP_CONTEXT]

Routing policy (hard rule):
  REQUEST_CATEGORY=LOCATION + ROUTE_TO=GEMINI_ONLY → Gemini CLI only.
  No other model may handle the request. No classifier. No ensemble.

Non-location APP_CONTEXT blocks are respected too: the block is stripped
from the message and normal routing continues with the clean message.

All functions never raise.
"""
import re

_BLOCK_RE = re.compile(r'\[APP_CONTEXT\](.*?)\[/APP_CONTEXT\]', re.DOTALL | re.IGNORECASE)


def parse_app_context(message: str) -> tuple[dict | None, str]:
    """
    Extract and parse the [APP_CONTEXT] block from a message.

    Returns:
        (metadata_dict, clean_message)  — if block found
        (None, original_message)        — if no block present
    """
    try:
        m = _BLOCK_RE.search(message)
        if not m:
            return None, message

        block = m.group(1).strip()
        meta = {}
        for line in block.splitlines():
            line = line.strip()
            if '=' in line:
                key, _, val = line.partition('=')
                meta[key.strip()] = val.strip()

        # Strip the block from the message
        clean = _BLOCK_RE.sub('', message).strip()
        # If nothing remains outside the block, use USER_QUERY as the clean message
        if not clean and meta.get('USER_QUERY'):
            clean = meta['USER_QUERY']

        return meta, clean
    except Exception:
        return None, message


def is_location_request(meta: dict) -> bool:
    """Return True if the metadata declares a location request for Gemini."""
    return (
        meta.get('REQUEST_CATEGORY', '').upper() == 'LOCATION'
        and meta.get('ROUTE_TO', '').upper() == 'GEMINI_ONLY'
    )


def build_location_prompt(meta: dict) -> str:
    """
    Build a rich Gemini prompt from the app-injected location metadata.

    Includes coordinates when LOCATION_PERMISSION=GRANTED, voice formatting
    when VOICE_MODE=true, and clear instructions when location is unavailable.
    Never fabricates coordinates.
    """
    lat    = meta.get('CURRENT_LAT', '')
    lon    = meta.get('CURRENT_LON', '')
    acc    = meta.get('LOCATION_ACCURACY', '')
    perm   = meta.get('LOCATION_PERMISSION', '').upper()
    voice  = meta.get('VOICE_MODE', 'false').lower() == 'true'
    query  = meta.get('USER_QUERY', 'What is my current location?')
    source = meta.get('DEVICE_LOCATION_SOURCE', 'device GPS')

    has_coords = perm == 'GRANTED' and lat and lon

    if has_coords:
        acc_str = f", accuracy: {acc}m" if acc else ""
        location_block = (
            f"The user's current GPS coordinates are: {lat}, {lon} "
            f"(source: {source}{acc_str})."
        )
    else:
        location_block = (
            "Location permission was not granted or coordinates are unavailable. "
            "Do NOT fabricate coordinates. State clearly that live location cannot be determined."
        )

    voice_instruction = (
        "\nVOICE MODE ACTIVE: Respond as a clean spoken answer. "
        "No markdown. No bullet points. Short, natural sentences only."
        if voice else ""
    )

    format_section = (
        "Voice version: [clean spoken response]"
        if voice
        else "Guidance: [short actionable steps]"
    )

    return f"""You are a location-aware assistant with real-world geographic knowledge.

{location_block}{voice_instruction}

User query: {query}

RESPONSE RULES:
- Never fabricate coordinates, place names, or distances.
- If coordinates are unavailable, say so clearly and offer general guidance only.
- Be precise, concise, and actionable.
- Use this format:

Current location: [brief description of where the user is]
Best match near you: [name of place, estimated distance, direction]
{format_section}"""
