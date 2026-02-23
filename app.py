import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

import pytz
from dotenv import load_dotenv
from flask import Flask, render_template, request, flash, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── App & DB ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///reminders.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

IST = pytz.timezone("Asia/Kolkata")

# ── Model ─────────────────────────────────────────────────────────────────────
class Reminder(db.Model):
    __tablename__ = "reminders"

    id        = db.Column(db.Integer, primary_key=True)
    email     = db.Column(db.String(255), nullable=False)
    message   = db.Column(db.Text, nullable=False)
    remind_at = db.Column(db.DateTime, nullable=False)
    sent      = db.Column(db.Boolean, default=False, nullable=False)

    def __repr__(self):
        return f"<Reminder id={self.id} email={self.email} sent={self.sent}>"


# ── Email Helper ──────────────────────────────────────────────────────────────
def send_email(to_email: str, message: str) -> bool:
    """Send a reminder email via Gmail SMTP. Returns True on success."""
    email_user = os.environ.get("EMAIL_USER")
    email_pass = os.environ.get("EMAIL_PASS")

    if not email_user or not email_pass:
        logger.error("EMAIL_USER or EMAIL_PASS not configured.")
        return False

    subject = "⏰ Smart Reminder"
    body = (
        f"Hello,\n\n"
        f"This is your reminder:\n\n"
        f"  {message}\n\n"
        f"—\nSent automatically by SmartReminder"
    )

    msg = MIMEMultipart()
    msg["From"]    = email_user
    msg["To"]      = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(email_user, email_pass)
            server.sendmail(email_user, to_email, msg.as_string())
        logger.info("Email sent to %s", to_email)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP authentication failed. Check EMAIL_USER / EMAIL_PASS.")
    except smtplib.SMTPException as exc:
        logger.error("SMTP error: %s", exc)
    except Exception as exc:
        logger.error("Unexpected error sending email: %s", exc)
    return False


# ── Scheduler Job ─────────────────────────────────────────────────────────────
def check_and_send_reminders():
    """Runs every SCHEDULER_INTERVAL seconds inside the Flask app context."""
    with app.app_context():
        now_utc = datetime.utcnow()
        # Convert UTC now to IST for comparison (DB stores naive IST datetimes)
        now_ist = datetime.now(IST).replace(tzinfo=None)

        pending = Reminder.query.filter(
            Reminder.remind_at <= now_ist,
            Reminder.sent == False,  # noqa: E712
        ).all()

        if not pending:
            logger.debug("No pending reminders at %s IST", now_ist.strftime("%Y-%m-%d %H:%M:%S"))
            return

        for reminder in pending:
            logger.info("Processing reminder id=%s for %s", reminder.id, reminder.email)
            success = send_email(reminder.email, reminder.message)
            if success:
                reminder.sent = True
                db.session.commit()
                logger.info("Reminder id=%s marked as sent.", reminder.id)
            else:
                logger.warning("Failed to send reminder id=%s, will retry next cycle.", reminder.id)


def scheduler_error_listener(event):
    logger.error("Scheduler job crashed: %s", event.exception)


# ── Scheduler Bootstrap ───────────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
interval  = int(os.environ.get("SCHEDULER_INTERVAL", 60))
scheduler.add_job(
    check_and_send_reminders,
    trigger="interval",
    seconds=interval,
    id="reminder_checker",
    replace_existing=True,
    max_instances=1,
)
scheduler.add_listener(scheduler_error_listener, EVENT_JOB_ERROR)


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        email      = request.form.get("email", "").strip()
        message    = request.form.get("message", "").strip()
        remind_str = request.form.get("remind_at", "").strip()

        # Validation
        errors = []
        if not email:
            errors.append("Email address is required.")
        if not message:
            errors.append("Reminder message is required.")
        if not remind_str:
            errors.append("Date & time is required.")

        remind_dt = None
        if remind_str:
            try:
                remind_dt = datetime.strptime(remind_str, "%Y-%m-%dT%H:%M")
                now_ist   = datetime.now(IST).replace(tzinfo=None)
                if remind_dt <= now_ist:
                    errors.append("Scheduled time must be in the future.")
            except ValueError:
                errors.append("Invalid date/time format.")

        if errors:
            for err in errors:
                flash(err, "danger")
            return redirect(url_for("index"))

        reminder = Reminder(email=email, message=message, remind_at=remind_dt)
        db.session.add(reminder)
        db.session.commit()
        logger.info("Reminder id=%s scheduled for %s → %s", reminder.id, email, remind_dt)

        flash(
            f"✅ Reminder set! We'll email <strong>{email}</strong> on "
            f"<strong>{remind_dt.strftime('%d %b %Y at %I:%M %p')}</strong> (IST).",
            "success",
        )
        return redirect(url_for("index"))

    return render_template("index.html")


# ── App Factory / Entry ───────────────────────────────────────────────────────
def create_tables():
    with app.app_context():
        db.create_all()
        logger.info("Database tables created / verified.")


if __name__ == "__main__":
    create_tables()
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started (interval=%ss).", interval)
    try:
        app.run(debug=False, host="0.0.0.0", port=5000)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
else:
    # Gunicorn entry point
    create_tables()
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started via Gunicorn (interval=%ss).", interval)