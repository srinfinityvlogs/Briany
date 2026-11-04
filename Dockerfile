FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
<<<<<<< HEAD
COPY phase1_checks.py llm_suite.py ./
=======
COPY phase1_checks.py .
>>>>>>> 6dd5923c850a60a53b115824a1bdf767d9492394
# Override the command in Northflank to pass --date for the accuracy suite
CMD ["python", "phase1_checks.py", "all", "--label", "northflank"]
