FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY ai_analysis.py delivery.py downstream_route.py human_decision.py human_resume.py human_resume_consumers.py intake.py main.py operations.py policy_retrieval.py portal.py recovery.py safe_action.py workflow_start.py ./

RUN useradd --create-home --uid 10001 service-request
USER service-request

CMD ["python", "delivery.py"]
