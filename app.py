"""
Strava → Bluesky Bot
Listens for new Strava activities via webhook and auto-posts to Bluesky.
"""

import os
import json
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

app = Flask(__name__)

# ─────────────────────────────────────────────
# CONFIG (loaded from .env)
# ─────────────────────────────────────────────
STRAVA_CLIENT_ID     = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN")
STRAVA_VERIFY_TOKEN  = os.getenv("STRAVA_VERIFY_TOKEN", "my_secret_verify_token")

BLUESKY_HANDLE       = os.getenv("BLUESKY_HANDLE")   # e.g. yourname.bsky.social
BLUESKY_APP_PASSWORD = os.getenv("BLUESKY_APP_PASSWORD")


# ─────────────────────────────────────────────
# STRAVA HELPERS
# ─────────────────────────────────────────────
def get_strava_access_token():
    """Exchange refresh token for a fresh access token."""
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id":     STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": STRAVA_REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_activity(activity_id):
    """Fetch full activity details from Strava."""
    token = get_strava_access_token()
    resp = requests.get(
        f"https://www.strava.com/api/v3/activities/{activity_id}",
        headers={"Authorization": f"Bearer {token}"}
    )
    resp.raise_for_status()
    return resp.json()


def format_post(activity):
    """Turn a Strava activity dict into a Bluesky post string."""
    name      = activity.get("name", "Workout")
    a_type    = activity.get("sport_type") or activity.get("type", "Activity")
    distance  = activity.get("distance", 0)          # meters
    moving    = activity.get("moving_time", 0)        # seconds
    elevation = activity.get("total_elevation_gain", 0)  # meters
    calories  = activity.get("calories")
    hr        = activity.get("average_heartrate")
    kudos     = activity.get("kudos_count", 0)

    # Convert units
    miles     = distance / 1609.34
    km        = distance / 1000
    hours, remainder = divmod(moving, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours > 0:
        duration_str = f"{int(hours)}h {int(minutes)}m {int(seconds)}s"
    else:
        duration_str = f"{int(minutes)}m {int(seconds)}s"

    # Pace (min/mile) — only for runs/walks
    run_types = {"Run", "TrailRun", "Walk", "Hike", "VirtualRun"}
    if a_type in run_types and miles > 0:
        pace_secs = moving / miles
        pace_min, pace_sec = divmod(pace_secs, 60)
        pace_str = f"{int(pace_min)}:{int(pace_sec):02d}/mi"
    else:
        pace_str = None

    # Pick a sport emoji
    emoji_map = {
        "Run": "🏃", "TrailRun": "🏃", "Walk": "🚶", "Hike": "🥾",
        "Ride": "🚴", "VirtualRide": "🚴", "GravelRide": "🚴",
        "Swim": "🏊", "WeightTraining": "🏋️", "Yoga": "🧘",
        "Workout": "💪", "Crossfit": "💪", "Rowing": "🚣",
        "Kayaking": "🛶", "Skiing": "⛷️", "Snowboard": "🏂",
        "Soccer": "⚽", "Tennis": "🎾", "Golf": "⛳",
    }
    emoji = emoji_map.get(a_type, "🏅")

    lines = [f"{emoji} {name}"]
    lines.append(f"📍 {a_type}")

    if miles >= 0.1:
        lines.append(f"📏 {miles:.2f} mi ({km:.2f} km)")
    if moving:
        lines.append(f"⏱️ {duration_str}")
    if pace_str:
        lines.append(f"💨 {pace_str}")
    if elevation:
        elev_ft = elevation * 3.28084
        lines.append(f"⛰️ {elev_ft:.0f} ft gain")
    if hr:
        lines.append(f"❤️ {hr:.0f} bpm avg HR")
    if calories:
        lines.append(f"🔥 {int(calories)} cal")

    lines.append("#Strava")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# BLUESKY HELPERS
# ─────────────────────────────────────────────
def bluesky_post(text):
    """Authenticate with Bluesky and create a post."""
    # Step 1: Create a session
    session_resp = requests.post(
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        json={"identifier": BLUESKY_HANDLE, "password": BLUESKY_APP_PASSWORD}
    )
    session_resp.raise_for_status()
    session = session_resp.json()
    access_jwt = session["accessJwt"]
    did        = session["did"]

    # Step 2: Create the post record
    post_resp = requests.post(
        "https://bsky.social/xrpc/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {access_jwt}"},
        json={
            "repo":       did,
            "collection": "app.bsky.feed.post",
            "record": {
                "$type":     "app.bsky.feed.post",
                "text":      text,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
        }
    )
    post_resp.raise_for_status()
    return post_resp.json()


# ─────────────────────────────────────────────
# WEBHOOK ROUTES
# ─────────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """
    Strava sends a GET request to verify your webhook endpoint.
    This only needs to happen once during setup.
    """
    challenge    = request.args.get("hub.challenge")
    verify_token = request.args.get("hub.verify_token")

    if verify_token == STRAVA_VERIFY_TOKEN:
        print("✅ Webhook verified by Strava.")
        return jsonify({"hub.challenge": challenge})
    else:
        print("❌ Invalid verify token from Strava.")
        return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook_event():
    """
    Strava sends a POST request whenever an event occurs
    (new activity, updated activity, etc.)
    """
    event = request.get_json(silent=True)
    print(f"📬 Received Strava event: {json.dumps(event, indent=2)}")

    # We only care about new activities (not updates or deletions)
    if event.get("object_type") == "activity" and event.get("aspect_type") == "create":
        activity_id = event.get("object_id")
        print(f"🏅 New activity detected: {activity_id}")

        try:
            activity = get_activity(activity_id)
            post_text = format_post(activity)
            print(f"📝 Posting to Bluesky:\n{post_text}")
            result = bluesky_post(post_text)
            print(f"✅ Posted! URI: {result.get('uri')}")
        except Exception as e:
            print(f"❌ Error: {e}")

    return "OK", 200


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"🚀 Starting Strava→Bluesky bot on port {port}")
    app.run(host="0.0.0.0", port=port)
