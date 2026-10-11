**1. Indexes:** I created unique indexes on `emp_code` and `(emp_code, date)` to prevent duplicate records. I used `(date, emp_code)` for attendance listing and monthly analytics, `(emp_code, punch_in)` for punch-out, and `(department, joined_on)` for headcount. I skipped a separate `status` index because it has few distinct values.

**2. Punch-in race:** If two requests come at the same time, the unique index on `(emp_code, date)` allows only one insert. The other request gets a duplicate-key error, which I handle by returning a 409 response.

**3. Ties:** I used `$rank` on total late minutes, so employees with equal values get the same rank and the next rank is skipped. Filtering with `rank <= limit` includes everyone tied at the cutoff.

**4. Headcount:** I started the aggregation from `employees` and used `$lookup` to fetch attendance logs. Filtering employees who joined on or before the last day of the month ensures that employees with no logs are also counted.

**5. Handling 100x data:** I would maintain monthly summary documents for each employee to reduce analytics query time. However, keeping them updated during punch-outs and corrections would make writes more complex.