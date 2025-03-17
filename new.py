import json
import re
import datetime
from flask import Flask, request, render_template_string
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.orm import declarative_base, sessionmaker
import sendgrid
from sendgrid.helpers.mail import Mail
import spacy
from dateutil import parser as date_parser
from googleapiclient.discovery import build
from google.oauth2 import service_account

# Initialize Flask app
app = Flask(__name__)

# Database setup with SQLAlchemy
Base = declarative_base()
engine = create_engine('sqlite:///scheduling.db', echo=True)
Session = sessionmaker(bind=engine)

# Explicitly create tables
print("Creating database tables...")
Base.metadata.create_all(engine)
print("Tables created.")

class SchedulingRequest(Base):
    __tablename__ = 'scheduling_requests'
    id = Column(Integer, primary_key=True)
    candidate_email = Column(String)
    recruiter_email = Column(String)
    status = Column(String, default='pending')
    availability = Column(String)  # JSON-encoded list of slots
# Email setup with SendGrid
SENDGRID_API_KEY = 'YOUR_SENDGRID_API_KEY'  # Replace with your SendGrid API key
SENDER_EMAIL = 'your_email@example.com'     # Replace with your verified sender email
sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)

def send_email(to, subject, body):
    message = Mail(
        from_email=SENDER_EMAIL,
        to_emails=to,
        subject=subject,
        plain_text_content=body
    )
    sg.send(message)

# NLP setup with spaCy
nlp = spacy.load("en_core_web_sm")
vague_times = {
    "morning": (8, 0, 12, 0),   # 8 AM - 12 PM
    "afternoon": (13, 0, 17, 0), # 1 PM - 5 PM
    "evening": (17, 0, 20, 0)    # 5 PM - 8 PM
}

def parse_availability(text):
    doc = nlp(text)
    availability = []
    current_date = None
    prev_time = None

    for ent in doc.ents:
        if ent.label_ == "DATE":
            try:
                current_date = date_parser.parse(ent.text, fuzzy=True, default=datetime.datetime.now()).date()
            except ValueError:
                current_date = None
        elif ent.label_ == "TIME":
            if current_date:
                time_text = ent.text.lower()
                if time_text in vague_times:
                    start_hour, start_min, end_hour, end_min = vague_times[time_text]
                    start_dt = datetime.datetime.combine(current_date, datetime.time(start_hour, start_min))
                    end_dt = datetime.datetime.combine(current_date, datetime.time(end_hour, end_min))
                    availability.append((start_dt, end_dt))
                else:
                    try:
                        time = date_parser.parse(time_text, fuzzy=True).time()
                        start_dt = datetime.datetime.combine(current_date, time)
                        if prev_time and prev_time[0] == current_date:
                            # Handle time range (e.g., "from 10 AM to 12 PM")
                            end_dt = start_dt
                            start_dt = prev_time[1]
                            availability.append((start_dt, end_dt))
                            prev_time = None
                        else:
                            # Single time, assume 1-hour slot
                            end_dt = start_dt + datetime.timedelta(hours=1)
                            availability.append((start_dt, end_dt))
                            prev_time = (current_date, start_dt)
                    except ValueError:
                        prev_time = None
            else:
                prev_time = None
    return availability

# Google Calendar setup
SCOPES = ['https://www.googleapis.com/auth/calendar']
SERVICE_ACCOUNT_FILE = 'service_account.json'  # Path to your service account key

def get_calendar_service(email):
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES).with_subject(email)
    return build('calendar', 'v3', credentials=credentials)

def get_free_slots(service, calendar_id, time_min, time_max):
    body = {
        "timeMin": time_min.isoformat() + 'Z',
        "timeMax": time_max.isoformat() + 'Z',
        "items": [{"id": calendar_id}]
    }
    freebusy = service.freebusy().query(body=body).execute()
    busy_times = freebusy['calendars'][calendar_id]['busy']
    return [(date_parser.parse(b['start']), date_parser.parse(b['end'])) for b in busy_times]

def find_overlapping_slot(availability, busy_slots, duration_hours=1):
    duration = datetime.timedelta(hours=duration_hours)
    for start, end in availability:
        slot_busy = [b for b in busy_slots if b[0] < end and b[1] > start]
        if not slot_busy:
            if (end - start) >= duration:
                return start, start + duration
        else:
            free_start = start
            for busy_start, busy_end in sorted(slot_busy, key=lambda x: x[0]):
                if (busy_start - free_start) >= duration:
                    return free_start, free_start + duration
                free_start = busy_end
            if (end - free_start) >= duration:
                return free_start, free_start + duration
    return None, None

def create_event(service, calendar_id, start, end, attendees):
    event = {
        'summary': 'Interview',
        'start': {'dateTime': start.isoformat() + 'Z', 'timeZone': 'UTC'},
        'end': {'dateTime': end.isoformat() + 'Z', 'timeZone': 'UTC'},
        'attendees': [{'email': email} for email in attendees],
    }
    service.events().insert(calendarId=calendar_id, body=event, sendUpdates='all').execute()

def schedule_interview(req):
    availability = json.loads(req.availability or '[]')
    if not availability:
        return False
    availability = [(date_parser.parse(start), date_parser.parse(end)) for start, end in availability]
    service = get_calendar_service(req.recruiter_email)
    calendar_id = 'primary'
    
    for start, end in availability:
        busy_slots = get_free_slots(service, calendar_id, start, end)
        slot_start, slot_end = find_overlapping_slot([(start, end)], busy_slots)
        if slot_start and slot_end:
            create_event(service, calendar_id, slot_start, slot_end, [req.recruiter_email, req.candidate_email])
            req.status = 'scheduled'
            with Session() as session:
                session.merge(req)
                session.commit()
            send_email(req.candidate_email, f"Interview Scheduled - Request #{req.id}",
                       f"Your interview is scheduled from {slot_start} to {slot_end} UTC.")
            return True
    send_email(req.candidate_email, f"No Available Slots - Request #{req.id}",
               "We couldn't find a matching slot. Please provide more availability.")
    return False

# Flask routes
@app.route('/')
def index():
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Schedule Interview</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; }
            h1 { color: #333; }
            form { max-width: 400px; }
            label { display: block; margin: 10px 0 5px; }
            input[type="email"] { width: 100%; padding: 8px; margin-bottom: 10px; }
            input[type="submit"] { background: #007BFF; color: white; padding: 10px 20px; border: none; cursor: pointer; }
            input[type="submit"]:hover { background: #0056b3; }
        </style>
    </head>
    <body>
        <h1>Schedule an Interview</h1>
        <form action="/schedule" method="post">
            <label for="candidate_email">Candidate Email:</label>
            <input type="email" id="candidate_email" name="candidate_email" required>
            <label for="recruiter_email">Recruiter Email:</label>
            <input type="email" id="recruiter_email" name="recruiter_email" required>
            <input type="submit" value="Send Availability Request">
        </form>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route('/schedule', methods=['POST'])
def schedule():
    candidate_email = request.form['candidate_email']
    recruiter_email = request.form['recruiter_email']
    with Session() as session:
        req = SchedulingRequest(candidate_email=candidate_email, recruiter_email=recruiter_email)
        session.add(req)
        session.commit()
        request_id = req.id
    send_email(candidate_email, f"Please provide your availability - Request #{request_id}",
               "Hi, please reply with your available times for the interview (e.g., 'Monday 10 AM to 12 PM').")
    return f"Request #{request_id} created and email sent to {candidate_email}."

@app.route('/incoming_email', methods=['POST'])
def incoming_email():
    subject = request.form.get('subject', '')
    text = request.form.get('text', '')
    match = re.search(r'Request #(\d+)', subject)
    if match:
        request_id = int(match.group(1))
        with Session() as session:
            req = session.query(SchedulingRequest).get(request_id)
            if req and req.status == 'pending':
                availability = parse_availability(text)
                req.availability = json.dumps([(start.isoformat(), end.isoformat()) for start, end in availability])
                session.commit()
                schedule_interview(req)
    return "OK"

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)