from celery import Celery
app = Celery("tasks", broker="redis://localhost:6379/0")

@app.task
def send_email(to: str):
    print(f"Sending to {to}")
