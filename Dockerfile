FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY phase1_checks.py .
# Override the command in Northflank to pass --date for the accuracy suite
CMD ["python", "phase1_checks.py", "all", "--label", "northflank"]
