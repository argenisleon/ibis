SELECT *, `foo` * 2 AS `qux`
FROM (
  SELECT *, `foo` + `bar` AS `baz`
  FROM tbl
) t0
