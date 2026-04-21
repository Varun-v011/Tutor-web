# test_email.py — run from project root
import sys
sys.path.insert(0, '.')

from services.email_service import send_booking_confirmation
from datetime import datetime, timezone

result = send_booking_confirmation(
    student_email="jgvvschool2015@gmail.com",
    student_name="Test Student",
    language="Japanese",
    meet_link="https://meet.google.com/test",
    booking_start=datetime.now(timezone.utc),
    graded_result=None,
)
print("✅ Sent!" if result else "❌ Failed — check logs above")