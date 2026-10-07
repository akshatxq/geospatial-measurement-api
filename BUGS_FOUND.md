# Bugs found during implementation

## Request limit originally surfaced as HTTP 400

The first raw-request limit raised a custom exception while FastAPI was parsing multipart data. FastAPI converted that parser exception into HTTP 400 before the middleware could convert it to 413.

The limit now raises Starlette's HTTPException(413), which FastAPI preserves. `test_body_limit_before_multipart` checks a body beyond the limit and verifies 413. This was found and fixed before the first project commit.

A separate test-helper argument naming collision initially prevented the missing-CRS override test from running; renaming the helper's file-content argument fixed the test. This was not an API bug.
