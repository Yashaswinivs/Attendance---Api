# Employee Attendance & Analytics API

FastAPI + MongoDB backend for a small HR attendance system: punch-in / punch-out with business rules, manual corrections with an audit trail, and MongoDB-backed analytics.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (tested on 7.0)

## Setup

1. Create a virtual environment:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   ```

2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Set MongoDB connection (optional; defaults are `mongodb://localhost:27017` and `attendance_db`):
   ```
   set MONGO_URI=mongodb://localhost:27017
   set MONGO_DB=attendance_db
   ```

4. (Optional) Load sample data:
   ```
   python sample_seed.py
   ```

5. Run the API:
   ```
   uvicorn app.main:app --port 8000
   ```

Then visit:
- Health: http://127.0.0.1:8000/health
- Swagger UI: http://127.0.0.1:8000/docs