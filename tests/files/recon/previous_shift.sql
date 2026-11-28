CREATE TEMP TABLE intake AS
  SELECT oid, cid, amount FROM orders;
SAVEPOINT sp1;
CREATE TEMP TABLE joined AS
  SELECT intake.oid, customers.name AS who, intake.amount
  FROM intake JOIN customers USING (cid);
SELECT oid, who, amount FROM joined;
