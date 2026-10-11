# Employee Attendance & Analytics API

An attendance API built with FastAPI and MongoDB (PyMongo). All code is in `app/main.py`.

## Run

1. Create and activate a virtual environment, then install the packages:
```
   python -m venv venv
   pip install -r requirements.txt
```
2. Set two environment variables (a local `.env` file also works):
   - `MONGO_URI`: your MongoDB connection string
   - `MONGO_DB`: the database name (for example `attendance_db`)
3. Start the server:
```
   uvicorn app.main:app --port 8000
```
4. Open `http://127.0.0.1:8000/docs` to try the endpoints.

Indexes are created automatically at startup. MongoDB 6.0 or newer is required.

## Status

All endpoints in the contract are implemented: health, employees, punch-in, punch-out, attendance listing, corrections with an audit trail, the four analytics endpoints (aggregation pipelines), and the explain endpoint.

I tested them against MongoDB Atlas with the nine sample documents, on Python 3.10, and checked the analytics numbers by hand. `/admin/explain` shows index scans and no collection scans for all five endpoints.

**Not tested:**
- Performance on a dataset of about 100,000 records (I only used the sample data).
- Truly simultaneous requests. Punch-in relies on a unique index, and punch-out and corrections use conditional updates, but I did not load-test them.

## Assumptions

- `moving_avg_7d` is the mean of the unrounded daily attendance rates, rounded to 4 decimals at the end.
- Punch-out and corrections validate the time range before checking for a conflict, so an invalid time returns 422 even on an already closed record.